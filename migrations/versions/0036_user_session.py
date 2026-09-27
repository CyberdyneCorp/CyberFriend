"""The web user area: user sessions, link notices, and suggestions from the web.

*   `user_session` is the server half of the `__Host-cf_user` cookie, the
    same shape as `admin_session` (0031) without roles and with
    `fresh_auth_at`: the `auth_time` of the last sign-in made with
    `max_age`, which "delete everything" on the web requires to be within
    five minutes. The id is stored as its sha256 and the tokens as AES-GCM
    ciphertext under `ADMIN_SESSION_KEY`. It is keyed on the CyberdyneAuth
    `sub`; which person that is comes from `person_account_link` on every
    request, so unlinking ends access at once.
*   `person_account_link` gains `email_hint`, the masked address the bot
    names in "Linked to a***@example.com, not you? [Unlink]" (never the
    address itself), and `notified_at`, when that message was sent. The bot
    sends it for every link still NULL here.
*   `feature_request.source_kind` accepts `web`: a suggestion made in the
    user area.

`purge_person_derived` is restated with the user sessions of the person's
linked subject deleted before the link itself, so opt-out and "delete
everything" end every web session too.

Revision ID: 0036
Revises: 0035
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

#: `purge_person_derived` as 0035 left it. Restated rather than imported so this
#: revision stays what it was when applied.
PURGES_0035 = (
    "DELETE FROM conversation_turn WHERE person_id = p_person_id",
    "DELETE FROM conversation_summary WHERE person_id = p_person_id",
    "DELETE FROM person_fact WHERE person_id = p_person_id",
    "DELETE FROM notification WHERE person_id = p_person_id",
    "DELETE FROM position_alert WHERE person_id = p_person_id",
    "DELETE FROM scheduled_task WHERE person_id = p_person_id",
    """DELETE FROM mcp_token t
        USING person_platform_id p
        WHERE p.person_id = p_person_id
          AND t.platform = p.platform
          AND t.platform_user_id = p.platform_user_id""",
    "DELETE FROM feature_request WHERE person_id = p_person_id",
    "DELETE FROM erasure_request WHERE person_id = p_person_id AND completed_at IS NOT NULL",
    "DELETE FROM person_account_link WHERE person_id = p_person_id",
    "DELETE FROM account_link_code WHERE person_id = p_person_id",
    "DELETE FROM account_provisioning_request WHERE person_id = p_person_id",
    "DELETE FROM account_consent WHERE person_id = p_person_id",
)

_LINK_INDEX = PURGES_0035.index("DELETE FROM person_account_link WHERE person_id = p_person_id")

#: The user sessions go first: they are found through the link.
PURGES = (
    *PURGES_0035[:_LINK_INDEX],
    """DELETE FROM user_session s
        USING person_account_link l
        WHERE l.person_id = p_person_id
          AND s.sub = l.sub""",
    *PURGES_0035[_LINK_INDEX:],
)

SOURCE_KINDS_0030 = ("command", "dm", "channel")
SOURCE_KINDS = (*SOURCE_KINDS_0030, "web")


def _purge_function(statements: tuple[str, ...]) -> str:
    return (
        "CREATE OR REPLACE FUNCTION purge_person_derived(p_person_id bigint) "
        "RETURNS void AS $$\nBEGIN\n"
        + "".join(f"    {statement};\n" for statement in statements)
        + "END;\n$$ LANGUAGE plpgsql"
    )


def _source_check(kinds: tuple[str, ...]) -> None:
    op.drop_constraint("ck_feature_request_source", "feature_request", type_="check")
    quoted = ", ".join(f"'{kind}'" for kind in kinds)
    op.create_check_constraint(
        "ck_feature_request_source", "feature_request", f"source_kind IN ({quoted})"
    )


def upgrade() -> None:
    op.create_table(
        "user_session",
        sa.Column("id_hash", sa.Text, primary_key=True),
        sa.Column("sub", sa.Text, nullable=False),
        sa.Column("email", sa.Text),
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
        sa.Column("fresh_auth_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_user_session_sub", "user_session", ["sub"])

    op.add_column("person_account_link", sa.Column("email_hint", sa.Text))
    op.add_column(
        "person_account_link", sa.Column("notified_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "ix_person_account_link_unnotified",
        "person_account_link",
        ["linked_at"],
        postgresql_where=sa.text("notified_at IS NULL"),
    )

    _source_check(SOURCE_KINDS)
    op.execute(_purge_function(PURGES))


def downgrade() -> None:
    op.execute(_purge_function(PURGES_0035))
    # A web suggestion was typed in the user area; before 0036 the nearest
    # kind is a command, which is what it was.
    op.execute("UPDATE feature_request SET source_kind = 'command' WHERE source_kind = 'web'")
    _source_check(SOURCE_KINDS_0030)
    op.drop_index("ix_person_account_link_unnotified", table_name="person_account_link")
    op.drop_column("person_account_link", "notified_at")
    op.drop_column("person_account_link", "email_hint")
    op.drop_index("ix_user_session_sub", table_name="user_session")
    op.drop_table("user_session")
