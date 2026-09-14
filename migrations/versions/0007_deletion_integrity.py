"""Make a deletion durable regardless of when the message arrives.

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Live messages are published to a queue and written later; deletions go
    # straight to the database. Delete a message before its insert lands and
    # `UPDATE message SET deleted_at ... WHERE id = :id` matched zero rows --
    # then the insert arrived and the retracted message was live forever.
    #
    # The tombstone therefore gets a home of its own, keyed on the platform
    # message id and referencing nothing: recording a withdrawal for a message
    # the corpus has never seen is the whole point, so a foreign key to
    # `message` would reintroduce the defect as a constraint violation. The
    # insert path reads this table and is born dead when it finds a row, which
    # makes out-of-order arrival correct by construction rather than by luck.
    op.create_table(
        "message_tombstone",
        sa.Column("message_id", sa.BigInteger, primary_key=True, autoincrement=False),
        # Known only once the message itself has landed, hence nullable. It
        # lets a channel purge take its own tombstones with it instead of
        # leaving rows behind for content that no longer exists.
        sa.Column(
            "channel_id",
            sa.BigInteger,
            sa.ForeignKey("channel.id"),
            nullable=True,
        ),
        # When the content was withdrawn, as reported by the platform. Copied
        # onto `message.deleted_at` whenever the row exists or later appears.
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        # When we learned of it. Distinct from `deleted_at`: reconciliation
        # discovers deletions long after they happened, and an operator asking
        # "what did that outage cost us" needs both.
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_message_tombstone_channel",
        "message_tombstone",
        ["channel_id"],
        postgresql_where=sa.text("channel_id IS NOT NULL"),
    )

    # Existing tombstones are backfilled so the ledger is authoritative from
    # the moment it exists. Without this, a message deleted before this
    # migration and re-ingested after it would be judged only by the conflict
    # clause -- which is correct today, but leaves two sources of truth
    # disagreeing about every row written before now.
    op.execute(
        "INSERT INTO message_tombstone (message_id, channel_id, deleted_at) "
        "SELECT id, channel_id, deleted_at FROM message WHERE deleted_at IS NOT NULL "
        "ON CONFLICT (message_id) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_index("ix_message_tombstone_channel", table_name="message_tombstone")
    op.drop_table("message_tombstone")
