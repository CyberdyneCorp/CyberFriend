"""Who asked each exported trace, and a queue of people to search Langfuse for.

An opt-out withdrew the traces quoting a person's messages only when those
messages were deleted in Discord, and never the traces of questions the person
asked: nothing recorded who asked them. `trace_export.asker_platform_user_id`
records it from now on, so an opt-out can mark those traces by one indexed
lookup.

Traces exported before this revision have no asker. For those the ingest
process searches Langfuse by `userId` (the tracer has always sent the asker's
platform id there) and records what it finds as pending deletions.
`trace_asker_search` is the durable queue for that search: the admin console
records the opt-out, ingest holds the Langfuse keys, and this table is how one
tells the other. A row stays open until the search has read every page, so a
Langfuse outage delays the search rather than losing it.

People who opted out before this revision are queued here too, which reaches
the traces of questions they asked. It does not reach traces quoting their
messages: the earlier opt-out deleted their `message` rows, which is the only
link from a quoted message to its author. docs/operations.md names the manual
cleanup.

Revision ID: 0029
Revises: 0028
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

QUEUE_EXISTING_OPT_OUTS = """
INSERT INTO trace_asker_search (platform_user_id)
SELECT DISTINCT p.platform_user_id
FROM person_opt_out o
JOIN person_platform_id p ON p.person_id = o.person_id
ON CONFLICT (platform_user_id) DO NOTHING
"""


def upgrade() -> None:
    # Nullable: every trace exported before this revision has no recorded
    # asker, and a web-only run with no person would have none either.
    op.add_column(
        "trace_export",
        sa.Column("asker_platform_user_id", sa.BigInteger, nullable=True),
    )
    op.create_index(
        "ix_trace_export_asker",
        "trace_export",
        ["asker_platform_user_id"],
        postgresql_where=sa.text("asker_platform_user_id IS NOT NULL"),
    )

    op.create_table(
        "trace_asker_search",
        # The platform id alone, because that is all Langfuse holds: the
        # tracer sends it as `userId` with no platform.
        sa.Column("platform_user_id", sa.BigInteger, primary_key=True),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        # Set once every page of the search was read and its traces recorded
        # as pending. A repeated opt-out reopens the row.
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_trace_asker_search_open",
        "trace_asker_search",
        ["requested_at"],
        postgresql_where=sa.text("completed_at IS NULL"),
    )
    op.execute(QUEUE_EXISTING_OPT_OUTS)


def downgrade() -> None:
    op.drop_index("ix_trace_asker_search_open", table_name="trace_asker_search")
    op.drop_table("trace_asker_search")
    op.drop_index("ix_trace_export_asker", table_name="trace_export")
    op.drop_column("trace_export", "asker_platform_user_id")
