"""Make channel purge executable, and stop tombstones being un-withdrawn.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PURGE_CHANNEL -- the only mechanism for removing a channel's content when
    # it leaves indexing scope -- could not execute: deleting from message
    # violated message_mention's foreign key, so the whole transaction rolled
    # back and a de-scoped channel silently stayed searchable.
    op.drop_constraint(
        "message_mention_message_id_fkey", "message_mention", type_="foreignkey"
    )
    op.create_foreign_key(
        "message_mention_message_id_fkey",
        "message_mention",
        "message",
        ["message_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Same failure one table over: window_message referenced message without a
    # cascade, so purging a channel aborted even after the mention rows went.
    op.drop_constraint(
        "conversation_window_message_message_id_fkey",
        "conversation_window_message",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "conversation_window_message_message_id_fkey",
        "conversation_window_message",
        "message",
        ["message_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    for table, name in (
        ("message_mention", "message_mention_message_id_fkey"),
        ("conversation_window_message", "conversation_window_message_message_id_fkey"),
    ):
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, "message", ["message_id"], ["id"])
