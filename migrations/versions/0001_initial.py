"""Initial corpus schema.

Revision ID: 0001
Revises:
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from chatmemory.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Requires an image carrying pgvector. Coolify's "PostgreSQL with pgvector"
    # type has it; the stock postgres image does not.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    dims = get_settings().embedding_dimensions

    op.create_table(
        "person",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "person_platform_id",
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("platform_user_id", sa.BigInteger, nullable=False),
        sa.Column("person_id", sa.BigInteger, sa.ForeignKey("person.id"), nullable=False),
        sa.PrimaryKeyConstraint("platform", "platform_user_id"),
    )
    op.create_index("ix_person_platform_person", "person_platform_id", ["person_id"])

    op.create_table(
        "channel",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("is_indexed", sa.Boolean, nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "message",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("channel_id", sa.BigInteger, sa.ForeignKey("channel.id"), nullable=False),
        sa.Column("author_person_id", sa.BigInteger, sa.ForeignKey("person.id"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True)),
        # Tombstone. Deleted content must stop being returned everywhere.
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("reply_to_id", sa.BigInteger),
        sa.Column("thread_id", sa.BigInteger),
        sa.Column("search_tsv", postgresql.TSVECTOR(), nullable=True),
    )
    # Partial indexes: tombstoned rows are excluded from every read path, so
    # they should not occupy the indexes those paths use either.
    op.create_index(
        "ix_message_channel_time",
        "message",
        ["channel_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_message_thread",
        "message",
        ["thread_id"],
        postgresql_where=sa.text("thread_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_message_tsv",
        "message",
        ["search_tsv"],
        postgresql_using="gin",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "message_mention",
        sa.Column("message_id", sa.BigInteger, sa.ForeignKey("message.id"), nullable=False),
        sa.Column("person_id", sa.BigInteger, sa.ForeignKey("person.id"), nullable=False),
        sa.PrimaryKeyConstraint("message_id", "person_id"),
    )
    # Powers "who asked me" without scanning message text.
    op.create_index("ix_mention_person", "message_mention", ["person_id"])

    op.create_table(
        "window",
        sa.Column("id", sa.BigInteger, primary_key=True),
        # Denormalised so the permission predicate constrains the index scan
        # rather than filtering its output. Post-filtering an approximate scan
        # silently under-returns.
        sa.Column("channel_id", sa.BigInteger, sa.ForeignKey("channel.id"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("thread_id", sa.BigInteger),
        sa.Column("embedding", Vector(dims)),
        sa.Column("search_tsv", postgresql.TSVECTOR(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_window_channel_time",
        "window",
        ["channel_id", "starts_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_window_tsv",
        "window",
        ["search_tsv"],
        postgresql_using="gin",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        "CREATE INDEX ix_window_embedding ON window "
        "USING hnsw (embedding vector_cosine_ops) WHERE deleted_at IS NULL"
    )

    op.create_table(
        "window_message",
        sa.Column("window_id", sa.BigInteger, sa.ForeignKey("window.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("message_id", sa.BigInteger, sa.ForeignKey("message.id"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("window_id", "message_id"),
    )
    op.create_index("ix_window_message_message", "window_message", ["message_id"])

    op.create_table(
        "ingest_cursor",
        sa.Column("channel_id", sa.BigInteger, sa.ForeignKey("channel.id"), primary_key=True),
        # Oldest message imported so far. Backfill walks backwards from here.
        sa.Column("oldest_message_id", sa.BigInteger),
        sa.Column("backfill_complete", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    for table in (
        "ingest_cursor", "window_message", "window", "message_mention",
        "message", "channel", "person_platform_id", "person",
    ):
        op.drop_table(table)
