"""An index on who wrote a message, for person search.

"What did Bea say last week" filters messages by author and time before it
ranks anything, and "which Bea?" asks whether a person has any visible,
live message at all. Without this both scan every message in the viewer's
channels. Partial on `deleted_at IS NULL` because neither reads a tombstone.

Revision ID: 0023
Revises: 0022
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_message_author_time",
        "message",
        ["author_person_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_message_author_time", table_name="message")
