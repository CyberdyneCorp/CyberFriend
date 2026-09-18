"""What was exported to the trace store, so deleting it can follow.

Revision ID: 0016
Revises: 0015
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tracing copies message text into a second store that has no viewer
    # scoping and no tombstones. The corpus guarantees that deleted content
    # disappears everywhere, immediately; these two tables are how that
    # guarantee reaches across the copy.
    #
    # The two halves run in different processes -- the bot exports, `ingest`
    # handles on_message_delete -- and share only this database, so the
    # mapping has to be durable rather than in memory.
    op.create_table(
        "trace_export",
        sa.Column("trace_id", sa.Text, primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        # Set when a message the trace quotes is deleted; cleared by nothing.
        # Two columns rather than one status: "asked for" and "confirmed by
        # the destination" are different facts, and a retry needs to tell
        # them apart. A row with a request and no confirmation is the work
        # queue, which is what makes a failed deletion retried rather than
        # lost.
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The retry sweep's only scan: requested, not yet confirmed. Partial, so
    # it stays the size of the outstanding work rather than of the archive.
    op.create_index(
        "ix_trace_export_pending_deletion",
        "trace_export",
        ["deletion_requested_at"],
        postgresql_where=sa.text("deletion_requested_at IS NOT NULL AND deleted_at IS NULL"),
    )

    op.create_table(
        "trace_export_message",
        sa.Column(
            "trace_id",
            sa.Text,
            sa.ForeignKey("trace_export.trace_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Deliberately not a foreign key to `message`. A trace may quote a
        # message this deployment never stored -- a federated or web result
        # carries none -- and, more to the point, the row must outlive the
        # message row it refers to: it is read *because* that message was
        # deleted. A cascade here would delete the evidence of what to clean
        # up at exactly the moment it is needed.
        sa.Column("platform_message_id", sa.BigInteger, primary_key=True),
    )
    op.create_index(
        "ix_trace_export_message_message",
        "trace_export_message",
        ["platform_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_trace_export_message_message", table_name="trace_export_message")
    op.drop_table("trace_export_message")
    op.drop_index("ix_trace_export_pending_deletion", table_name="trace_export")
    op.drop_table("trace_export")
