"""The one-time tracing notice: `person.tracing_notice_version`, `person.tracing_notice_at`.

A person's questions and the assistant's answers are exported to Langfuse,
kept up to `TRACE_RETENTION_DAYS` and readable by admins. The first traced
reply a person gets carries a notice saying so; these columns record which
version of that notice they were shown and when, so it is shown once per
version. "Delete everything" clears both. Later, the admin console's usage
view will use `tracing_notice_at` to show a person's question text only for
traces exported after it; nothing reads it for that yet.

No new table, so `purge_person_derived` is unchanged: the columns hold a
number and a time, not anything the person said.

Revision ID: 0033
Revises: 0032
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("person", sa.Column("tracing_notice_version", sa.Integer))
    op.add_column("person", sa.Column("tracing_notice_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("person", "tracing_notice_at")
    op.drop_column("person", "tracing_notice_version")
