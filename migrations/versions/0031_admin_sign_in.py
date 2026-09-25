"""CyberdyneAuth sign-in: server-side sessions, login records, and who to display.

`admin_session` is the server half of the console's cookie: the browser holds
an opaque id and this row holds everything else. The id is stored as its
sha256, like `admin_token`, and the tokens are AES-GCM ciphertext under
`ADMIN_SESSION_KEY`, so the database alone yields no usable credential.

`admin_login` is one sign-in in flight: the state (as a hash), the nonce (as a
hash), the PKCE verifier (encrypted), and the hash of the value in the
`__Host-cf_login` cookie that binds the sign-in to the browser that started
it. `purpose`, `link_code_hash` and `max_age` are there for the user area's
`/link` and fresh sign-in, which reuse the same record.

`config_audit.operator_display` holds a signed-in person's email beside the
`oidc:<sub>` actor. ADD COLUMN does not touch the append-only trigger from
0012; a test asserts UPDATE and DELETE are still refused.

Revision ID: 0031
Revises: 0030
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_session",
        sa.Column("id_hash", sa.Text, primary_key=True),
        sa.Column("sub", sa.Text, nullable=False),
        sa.Column("email", sa.Text),
        # For display and review only. Every request takes its roles from the
        # verified access token, never from this column.
        sa.Column(
            "roles",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("access_token_enc", sa.LargeBinary, nullable=False),
        sa.Column("refresh_token_enc", sa.LargeBinary),
        sa.Column("id_token_enc", sa.LargeBinary, nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_admin_session_sub", "admin_session", ["sub"])

    op.create_table(
        "admin_login",
        sa.Column("state_hash", sa.Text, primary_key=True),
        sa.Column("nonce_hash", sa.Text, nullable=False),
        sa.Column("verifier_enc", sa.LargeBinary, nullable=False),
        sa.Column("binding_hash", sa.Text, nullable=False),
        sa.Column("purpose", sa.Text, nullable=False, server_default="admin"),
        sa.Column("link_code_hash", sa.Text),
        sa.Column("max_age", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_admin_login_expires_at", "admin_login", ["expires_at"])

    op.add_column("config_audit", sa.Column("operator_display", sa.Text))


def downgrade() -> None:
    op.drop_column("config_audit", "operator_display")
    op.drop_index("ix_admin_login_expires_at", table_name="admin_login")
    op.drop_table("admin_login")
    op.drop_index("ix_admin_session_sub", table_name="admin_session")
    op.drop_table("admin_session")
