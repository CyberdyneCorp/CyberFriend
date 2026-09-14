"""Track which channels need their windows re-formed.

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Window rebuilds were triggered by "this message belongs to no live
    # window", which can only ever fire once per message. An edited message is
    # already in a window, so its window kept the pre-edit text forever; and
    # consecutive messages each became their own single-message window, since
    # merging them would mean re-forming a window over already-windowed
    # messages -- which that trigger cannot express. Single-message windows
    # defeat the entire reason retrieval embeds windows rather than messages.
    #
    # The trigger is now a per-channel watermark: anything that changes a
    # channel's content marks it dirty from that point, and the rebuild
    # re-forms every window from there.
    op.add_column(
        "ingest_cursor",
        sa.Column("windows_dirty_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ingest_cursor_dirty",
        "ingest_cursor",
        ["windows_dirty_from"],
        postgresql_where=sa.text("windows_dirty_from IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ingest_cursor_dirty", table_name="ingest_cursor")
    op.drop_column("ingest_cursor", "windows_dirty_from")
