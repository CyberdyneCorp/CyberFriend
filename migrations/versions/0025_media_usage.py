"""Minutes of audio transcribed, per person and calendar month.

The ledger behind the voice-question caps: a person's monthly minutes, and the
deployment's, which is the sum of every row for the month. `purpose` keeps a
person's own questions apart from any later transcription of the voice notes
they post in channels, so the per-person cap counts only what they asked while
the monthly ceiling counts both.

Seconds only -- no audio, no transcript, no message id. A person deleted from
`person` takes their rows with them. Opt-out keeps them: the rows say nothing
the person said, and dropping them would hand the month's minutes back to the
deployment's ceiling.

Revision ID: 0025
Revises: 0024
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_usage",
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # The first day of the month, in UTC.
        sa.Column("month", sa.Date, primary_key=True),
        sa.Column("purpose", sa.Text, primary_key=True),
        sa.Column("seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("purpose IN ('question', 'channel')", name="ck_media_usage_purpose"),
        sa.CheckConstraint("seconds >= 0", name="ck_media_usage_seconds"),
    )
    # The monthly ceiling sums every row of one month.
    op.create_index("ix_media_usage_month", "media_usage", ["month"])


def downgrade() -> None:
    op.drop_table("media_usage")
