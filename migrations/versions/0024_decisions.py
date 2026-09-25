"""Extracted decisions: what a group settled on, and the messages it rests on.

Revision ID: 0024
Revises: 0023
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # From the environment rather than Settings, for the reason 0003 gives.
    dims = int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))

    op.create_table(
        "decision",
        sa.Column("id", sa.BigInteger, primary_key=True),
        # The source message and the topic's slug, never the summary: a model
        # rephrases its summary between runs, and re-extraction upserts on this.
        sa.Column("decision_key", sa.Text, nullable=False, unique=True),
        sa.Column(
            "source_message_id",
            sa.BigInteger,
            sa.ForeignKey("message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalised for the reason it is on `ask`: the viewer's channels
        # have to constrain the scan, and a decision is a summary of a
        # conversation, which leaks more than the quote it came from.
        sa.Column("channel_id", sa.BigInteger, sa.ForeignKey("channel.id"), nullable=False),
        sa.Column("thread_id", sa.BigInteger),
        # Who stated it: the source message's author.
        sa.Column(
            "author_person_id", sa.BigInteger, sa.ForeignKey("person.id"), nullable=False
        ),
        # The source and every message the model was shown with it. No foreign
        # key -- Postgres has none for array elements -- so reads and purges
        # check these themselves.
        sa.Column(
            "evidence_message_ids",
            postgresql.ARRAY(sa.BigInteger),
            nullable=False,
            server_default=sa.text("ARRAY[]::bigint[]"),
        ),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("topic", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        # The source message's timestamp: dates are how a later decision is
        # seen to supersede an earlier one, since nothing models that.
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        # Nullable: a decision whose embedding failed is still stored and
        # still found lexically.
        sa.Column("embedding", Vector(dims)),
        # 'simple', not 'english': decisions are written in Portuguese as
        # often as in English, and English stemming mangles the former.
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple'::regconfig, topic || ' ' || summary)", persisted=True
            ),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_decision_confidence"
        ),
    )
    op.create_index("ix_decision_channel_time", "decision", ["channel_id", "decided_at"])
    op.create_index("ix_decision_source", "decision", ["source_message_id"])
    op.create_index("ix_decision_tsv", "decision", ["search_tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_table("decision")
