"""Give the window-dirty watermark a generation counter.

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The rebuild read the watermark, rebuilt, then cleared it using the value
    # it had read. Because marks fold in with LEAST, a change arriving DURING
    # the rebuild left the watermark unchanged -- and the clear then discarded
    # it. The change was silently lost: an edit made while its channel was
    # rebuilding kept its pre-edit text until something else touched that
    # channel.
    #
    # The counter makes the clear conditional on nothing having happened in
    # between, so a mark that races the rebuild survives to the next pass.
    op.add_column(
        "ingest_cursor",
        sa.Column(
            "windows_dirty_seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ingest_cursor", "windows_dirty_seq")
