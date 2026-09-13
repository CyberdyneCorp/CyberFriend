"""Document corpus: documents, their entries, their chunks, and the fetch log.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from chatmemory.config import get_settings

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dims = get_settings().embedding_dimensions

    op.create_table(
        "document",
        sa.Column("id", sa.BigInteger, primary_key=True),
        # What makes this the same document across ingestions: the bytes for an
        # attachment, the URI for an external document. An external document's
        # content is expected to change underneath us, so keying it on a hash
        # would orphan its entries on every edit.
        sa.Column("identity_key", sa.Text, nullable=False, unique=True),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("byte_size", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("external_uri", sa.Text),
        # The source's own version marker, so reconciliation can tell a real
        # change from a re-fetch of the same content.
        sa.Column("source_revision", sa.Text),
        # Author-supplied strings. Stored as content and fenced as content:
        # nobody reads a PDF's /Keywords, which is what makes it a good place
        # to hide instructions.
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_document_external",
        "document",
        ["external_uri"],
        postgresql_where=sa.text("external_uri IS NOT NULL AND deleted_at IS NULL"),
    )

    # One act of sharing. Many-to-one: the same document may enter through
    # several messages and several channels, and each entry is an independent
    # disclosure decision made by the person who posted it.
    op.create_table(
        "document_entry",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger,
            sa.ForeignKey("document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger, sa.ForeignKey("channel.id"), nullable=False),
        # No foreign key to message: an attachment can be captured from a
        # backfill page before the message row lands, and a document that
        # survives its message's purge must still be withdrawable by id.
        sa.Column("message_id", sa.BigInteger, nullable=False),
        sa.Column("uploader_person_id", sa.BigInteger, sa.ForeignKey("person.id")),
        sa.Column("attachment_id", sa.BigInteger),
        # Empty string rather than NULL so the uniqueness constraint below
        # actually constrains: NULLs never conflict with each other.
        sa.Column("source_url", sa.Text, nullable=False, server_default=""),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "document_id", "message_id", "source_url", name="uq_document_entry_share"
        ),
    )
    op.create_index(
        "ix_document_entry_channel",
        "document_entry",
        ["channel_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_document_entry_message", "document_entry", ["message_id"])
    op.create_index(
        "ix_document_entry_uploader",
        "document_entry",
        ["uploader_person_id"],
        postgresql_where=sa.text("uploader_person_id IS NOT NULL"),
    )

    op.create_table(
        "document_chunk",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger,
            sa.ForeignKey("document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        # Where in the document this came from, for the citation: "p. 4",
        # "footnote 2", "paragraph 118".
        sa.Column("location", sa.Text, nullable=False, server_default=""),
        sa.Column("heading", sa.Text),
        # The permission predicate, denormalised onto the chunk. A join table
        # would put it on the other side of a join from the ranking, where
        # pgvector cannot use it to constrain an approximate scan -- and a
        # scan that returns its k best rows before the filter applies
        # under-returns silently, worst for people in the fewest channels.
        sa.Column(
            "channel_ids",
            postgresql.ARRAY(sa.BigInteger),
            nullable=False,
            server_default=sa.text("ARRAY[]::bigint[]"),
        ),
        # Same model, same dimensions, same space as conversation windows, so
        # one query matches either kind.
        sa.Column("embedding", Vector(dims)),
        sa.Column("search_tsv", postgresql.TSVECTOR(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_document_chunk_ordinal"),
    )
    op.create_index(
        "ix_document_chunk_channels",
        "document_chunk",
        ["channel_ids"],
        postgresql_using="gin",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_document_chunk_tsv",
        "document_chunk",
        ["search_tsv"],
        postgresql_using="gin",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        "CREATE INDEX ix_document_chunk_embedding ON document_chunk "
        "USING hnsw (embedding vector_cosine_ops) WHERE deleted_at IS NULL"
    )

    # The audit trail: what was fetched, from where, and which message linked
    # it. `outcome` is a coarse vocabulary on purpose -- "unretrievable" covers
    # missing, forbidden and refused, because recording which one is how the
    # distinction eventually reaches someone who should not have it.
    op.create_table(
        "document_fetch",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("target", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger, nullable=False),
        sa.Column("document_hash", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_document_fetch_target", "document_fetch", ["target"])


def downgrade() -> None:
    for table in ("document_fetch", "document_chunk", "document_entry", "document"):
        op.drop_table(table)
