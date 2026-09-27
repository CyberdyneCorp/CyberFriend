"""Account provisioning: consent, requests, link codes and links.

A person asks for a CyberdyneAuth account in a direct message, sees exactly
the name and email that will be sent, and confirms. These tables record that
and nothing more:

*   `account_consent`: that the person confirmed, which version of the consent
    text they were shown, and when. The email only as
    `HMAC-SHA256(PROVISIONING_EMAIL_KEY, lowercased email)`, never the address
    and never a plain hash, which could be reversed by trying likely
    addresses.
*   `account_provisioning_request`: one row per request sent to CyberdyneAuth,
    counted for the per-person limits (1 in 24 hours, 3 in 30 days). Ingest
    deletes rows older than 30 days, when they no longer count.
*   `account_link_code`: the single-use, 15-minute code the bot DMs so the
    person can prove on the web that the account is theirs. Only the code's
    sha256 is kept: the code itself is a bearer secret. Issuing a new code
    ends the earlier ones by moving their `expires_at` to the time of issue.
*   `person_account_link`: which CyberdyneAuth subject a person is linked to.
    Written by the web link flow, which arrives later; created here so the
    purge below covers every account table from the start.

Every table is personal, so each is added to `purge_person_derived` (0028):
opt-out and "delete everything" remove them through the same function, and
deleting the person cascades.

Revision ID: 0035
Revises: 0033
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035"
down_revision = "0033"
branch_labels = None
depends_on = None

HMAC_BYTES = 32
"""HMAC-SHA256 and sha256 are both 32 bytes; anything else is a bug."""

#: `purge_person_derived` as 0032 left it (0033 did not change it). Restated
#: rather than imported so this revision stays what it was when applied.
PURGES_0032 = (
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
)

PURGES = (
    *PURGES_0032,
    "DELETE FROM person_account_link WHERE person_id = p_person_id",
    "DELETE FROM account_link_code WHERE person_id = p_person_id",
    "DELETE FROM account_provisioning_request WHERE person_id = p_person_id",
    "DELETE FROM account_consent WHERE person_id = p_person_id",
)


def _purge_function(statements: tuple[str, ...]) -> str:
    return (
        "CREATE OR REPLACE FUNCTION purge_person_derived(p_person_id bigint) "
        "RETURNS void AS $$\nBEGIN\n"
        + "".join(f"    {statement};\n" for statement in statements)
        + "END;\n$$ LANGUAGE plpgsql"
    )


def _person_id() -> sa.Column[int]:
    return sa.Column(
        "person_id",
        sa.BigInteger,
        sa.ForeignKey("person.id", ondelete="CASCADE"),
        nullable=False,
    )


def _now(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _digest(name: str, table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"octet_length({name}) = {HMAC_BYTES}", name=f"ck_{table}_{name}_length"
    )


def upgrade() -> None:
    op.create_table(
        "account_consent",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _person_id(),
        sa.Column("consent_version", sa.SmallInteger, nullable=False),
        sa.Column("email_hmac", postgresql.BYTEA, nullable=False),
        _now("consented_at"),
        _digest("email_hmac", "account_consent"),
    )
    op.create_index(
        "ix_account_consent_person", "account_consent", ["person_id", "consented_at"]
    )

    op.create_table(
        "account_provisioning_request",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _person_id(),
        sa.Column("email_hmac", postgresql.BYTEA, nullable=False),
        _now("requested_at"),
        _digest("email_hmac", "account_provisioning_request"),
    )
    op.create_index(
        "ix_account_provisioning_request_person",
        "account_provisioning_request",
        ["person_id", "requested_at"],
    )
    op.create_index(
        "ix_account_provisioning_request_at", "account_provisioning_request", ["requested_at"]
    )

    op.create_table(
        "account_link_code",
        sa.Column("code_sha256", postgresql.BYTEA, primary_key=True),
        _person_id(),
        sa.Column("email_hmac", postgresql.BYTEA, nullable=False),
        _now("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        _digest("code_sha256", "account_link_code"),
        _digest("email_hmac", "account_link_code"),
    )
    op.create_index(
        "ix_account_link_code_person", "account_link_code", ["person_id", "created_at"]
    )
    op.create_index("ix_account_link_code_created", "account_link_code", ["created_at"])

    op.create_table(
        "person_account_link",
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("issuer", sa.Text, nullable=False),
        sa.Column("sub", sa.Text, nullable=False, unique=True),
        _now("linked_at"),
    )

    op.execute(_purge_function(PURGES))


def downgrade() -> None:
    op.execute(_purge_function(PURGES_0032))
    op.drop_table("person_account_link")
    op.drop_table("account_link_code")
    op.drop_table("account_provisioning_request")
    op.drop_table("account_consent")
