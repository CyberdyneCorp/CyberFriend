"""Voice notes and images attached to captured messages, pending processing.

One row per allowlisted attachment of a message in an indexed channel: what
the platform said about it and a signed CDN URL, never the bytes. Capture only
ever writes 'pending'; the later statuses and columns are filled by whatever
transcribes or describes the row.

The foreign key to `message` cascades, so retention, opt-out and a channel
purge -- which all delete messages -- take the rows with them, and no row can
exist for a message that was never stored (a DM, a private thread, a channel
out of scope, an opted-out author). A tombstone does not delete the message,
so it withdraws the row instead, in its own transaction.

Revision ID: 0026
Revises: 0025
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_media",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "message_id",
            sa.BigInteger,
            sa.ForeignKey("message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attachment_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("declared_type", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False, server_default=""),
        sa.Column("byte_size", sa.BigInteger, nullable=False),
        # Signed and expiring: refreshed while pending each time the message is
        # re-read, and emptied once the row is withdrawn.
        sa.Column("source_url", sa.Text, nullable=False),
        # The duration Discord declares on a voice note; a transcriber's own
        # figure replaces it.
        sa.Column("duration_secs", sa.Float),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        # Closed vocabulary, set with status 'skipped' or 'failed'.
        sa.Column("skip_reason", sa.Text),
        sa.Column("attempts", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # The derived text: NULL unless 'done', and NULLed on withdrawal.
        sa.Column("text", sa.Text),
        sa.Column("language", sa.Text),
        sa.Column("model", sa.Text),
        # How many secrets were redacted from `text`; never what they were.
        sa.Column("redactions", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("content_sha256", sa.Text),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("message_id", "attachment_id", name="uq_message_media_attachment"),
        sa.CheckConstraint("kind IN ('voice', 'audio', 'image')", name="ck_message_media_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'failed', 'skipped', 'withheld', 'withdrawn')",
            name="ck_message_media_status",
        ),
        sa.CheckConstraint(
            "text IS NULL OR status = 'done'", name="ck_message_media_text_only_when_done"
        ),
    )
    # What a worker claims next.
    op.create_index(
        "ix_message_media_pending",
        "message_media",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # The monthly budget sums what was processed this month.
    op.create_index(
        "ix_message_media_processed",
        "message_media",
        ["processed_at"],
        postgresql_where=sa.text("status = 'done'"),
    )


def downgrade() -> None:
    op.drop_table("message_media")
