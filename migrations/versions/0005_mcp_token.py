"""Bring the MCP token table into the schema.

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The table was created at start-up by `mcp.auth.ensure_token_schema`, so
    # it exists in every already-deployed database and in none of the backups
    # taken from a schema dump. Adopting it when present is what lets those
    # deployments reach this revision; creating it when absent is what makes a
    # restore come back with credentials still working.
    if sa.inspect(op.get_bind()).has_table("mcp_token"):
        return

    op.create_table(
        "mcp_token",
        # The token itself is never stored -- only its hash, so a database
        # disclosure does not hand out working credentials.
        sa.Column("token_hash", sa.Text, primary_key=True),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("platform_user_id", sa.BigInteger, nullable=False),
        sa.Column("label", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Revocation is a tombstone here too: a revoked credential stays on
        # record, so "this token was withdrawn" remains answerable.
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    # Partial, matching the only lookup that matters: authenticating a live
    # credential. Revoked rows are never read by it.
    op.create_index(
        "ix_mcp_token_person",
        "mcp_token",
        ["platform", "platform_user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_token_person", table_name="mcp_token")
    op.drop_table("mcp_token")
