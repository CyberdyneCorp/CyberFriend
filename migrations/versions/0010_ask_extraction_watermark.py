"""Track which messages have been through ask extraction.

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extraction only ever saw the live stream, because capture submitted
    # each message to the worker as it arrived. Everything imported by
    # backfill -- which is most of a channel's history -- was offered to
    # nothing, so "what did people ask me to do?" knew only what happened
    # while this process was running.
    #
    # Two counters rather than a done flag, for the reason migration 0009
    # gave the window watermark a generation: extraction takes model calls
    # and seconds, and an edit landing in that gap must not be swallowed by
    # the mark that follows it. `asks_extraction_seq` counts content
    # revisions and is bumped by the upsert; `asks_extracted_seq` records
    # which revision was actually extracted. A message is pending when the
    # two differ, so recording a generation that an edit has already moved
    # past leaves the message pending instead of clearing it -- no
    # conditional UPDATE, and no way to write the clear without the
    # generation in hand.
    op.add_column(
        "message",
        sa.Column(
            "asks_extraction_seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "message",
        # NULL means never extracted, which is deliberately what every row
        # already in the corpus gets: the history imported before this
        # existed is exactly the backlog the feature is for.
        sa.Column("asks_extracted_seq", sa.BigInteger(), nullable=True),
    )

    # The pass runs forever on channels that stopped changing months ago, so
    # "nothing to do" has to cost an empty index scan rather than a scan of
    # the corpus. The predicate is the pending test itself, so the planner
    # can use this index for the query and for its bounded backlog count.
    op.create_index(
        "ix_message_asks_pending",
        "message",
        [sa.text("created_at DESC")],
        postgresql_where=sa.text(
            "deleted_at IS NULL AND asks_extracted_seq IS DISTINCT FROM asks_extraction_seq"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_message_asks_pending", table_name="message")
    op.drop_column("message", "asks_extracted_seq")
    op.drop_column("message", "asks_extraction_seq")
