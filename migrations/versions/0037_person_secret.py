"""A person's own connected-app key: `person_secret`, sealed at rest.

A person hands CyberFriend their CyberWealth connected-app key in a DM. It is
stored here encrypted (AES-GCM under `PERSONAL_SECRETS_KEY`, with the person id
and the kind as associated data, so a ciphertext copied onto another person's
row does not open), next to its last four characters, the only part ever
shown back. One key per person and kind: connecting again replaces it.

It is personal data like any other. `purge_person_derived` deletes it, so an
opt-out and "Delete everything" both remove it in the same transaction as the
rest; a key saved by an opted-out person is dropped before it is stored, as
0030 does for suggestions; and deleting the person cascades.

Re-chaining: PURGES_0032 is the purge as of this revision's parent. If another
revision that rewrites `purge_person_derived` lands first, this one must move
after it and restate that revision's body plus the person_secret DELETE, or the
later CREATE OR REPLACE drops one side's DELETE lines.

Revision ID: 0037
Revises: 0033
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0033"
branch_labels = None
depends_on = None

KINDS = ("cyberwealth",)

GUARD = """
CREATE OR REPLACE FUNCTION reject_opted_out_person_secret() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM person_opt_out WHERE person_id = NEW.person_id
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

#: `purge_person_derived` as 0032 left it. Restated rather than imported so this
#: revision stays what it was when applied, whatever a later one does.
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
    "DELETE FROM person_secret WHERE person_id = p_person_id",
)


def _purge_function(statements: tuple[str, ...]) -> str:
    return (
        "CREATE OR REPLACE FUNCTION purge_person_derived(p_person_id bigint) "
        "RETURNS void AS $$\nBEGIN\n"
        + "".join(f"    {statement};\n" for statement in statements)
        + "END;\n$$ LANGUAGE plpgsql"
    )


def upgrade() -> None:
    op.create_table(
        "person_secret",
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("kind", sa.Text, primary_key=True),
        # nonce || AES-GCM ciphertext; never the key itself.
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("last4", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in KINDS) + ")",
            name="ck_person_secret_kind",
        ),
        sa.CheckConstraint("char_length(last4) = 4", name="ck_person_secret_last4"),
    )
    op.execute(GUARD)
    op.execute(
        "CREATE TRIGGER trg_person_secret_opt_out "
        "BEFORE INSERT ON person_secret "
        "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_person_secret()"
    )
    op.execute(_purge_function(PURGES))


def downgrade() -> None:
    op.execute(_purge_function(PURGES_0032))
    op.execute("DROP TRIGGER IF EXISTS trg_person_secret_opt_out ON person_secret")
    op.execute("DROP FUNCTION IF EXISTS reject_opted_out_person_secret()")
    op.drop_table("person_secret")
