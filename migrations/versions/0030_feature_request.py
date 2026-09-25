"""Feature requests: what people asked the assistant to learn to do.

One row per suggestion, holding the person's own words and nothing about the
conversation around them: no message text, no channel name, only the ids a
console can resolve itself.

Privacy follows the pattern of 0013 and 0014. A suggestion from an opted-out
person is dropped before it is stored, and deleting the person cascades.
Opt-out and self-service erasure both go through `purge_person_derived`
(0028), so this revision adds its DELETE to that function rather than a
trigger of its own: recording an opt-out deletes every suggestion the person
made, in the same transaction as the flag, and erasure reaches them too.

Revision ID: 0030
Revises: 0029
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

# Mirrors `ports.feature_requests`. In the database as well, so a statement
# written outside the service still cannot store a longer text or an unknown
# status.
MAX_TEXT_CHARS = 1000
SOURCE_KINDS = ("command", "dm", "channel")
STATUSES = ("new", "triaged", "planned", "done", "declined", "duplicate")

GUARD = """
CREATE OR REPLACE FUNCTION reject_opted_out_feature_request() RETURNS trigger AS $$
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

#: `purge_person_derived` as 0028 left it. Restated rather than imported so this
#: revision stays what it was when applied, whatever a later one does.
PURGES_0028 = (
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
)

PURGES = (
    *PURGES_0028,
    "DELETE FROM feature_request WHERE person_id = p_person_id",
)


def _purge_function(statements: tuple[str, ...]) -> str:
    return (
        "CREATE OR REPLACE FUNCTION purge_person_derived(p_person_id bigint) "
        "RETURNS void AS $$\nBEGIN\n"
        + "".join(f"    {statement};\n" for statement in statements)
        + "END;\n$$ LANGUAGE plpgsql"
    )


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "feature_request",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("normalized_hash", postgresql.BYTEA, nullable=False),
        sa.Column("language", sa.Text, nullable=True),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("guild_id", sa.BigInteger, nullable=True),
        sa.Column("channel_id", sa.BigInteger, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("admin_note", sa.Text, nullable=True),
        sa.Column(
            "duplicate_of",
            sa.BigInteger,
            sa.ForeignKey("feature_request.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "notify_on_change", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("notified_status", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_by", sa.Text, nullable=True),
        sa.CheckConstraint(
            f"char_length(text) BETWEEN 1 AND {MAX_TEXT_CHARS}",
            name="ck_feature_request_text_length",
        ),
        sa.CheckConstraint(
            f"source_kind IN ({_quoted(SOURCE_KINDS)})", name="ck_feature_request_source"
        ),
        sa.CheckConstraint(
            f"status IN ({_quoted(STATUSES)})", name="ck_feature_request_status"
        ),
        # A resubmission is the same row: the service answers with its number.
        sa.UniqueConstraint(
            "person_id", "normalized_hash", name="uq_feature_request_person_hash"
        ),
    )
    # The rolling daily limit counts one person's recent rows.
    op.create_index(
        "ix_feature_request_person_created", "feature_request", ["person_id", "created_at"]
    )
    op.create_index("ix_feature_request_status", "feature_request", ["status"])

    op.execute(GUARD)
    op.execute(
        "CREATE TRIGGER trg_feature_request_opt_out "
        "BEFORE INSERT OR UPDATE ON feature_request "
        "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_feature_request()"
    )
    op.execute(_purge_function(PURGES))


def downgrade() -> None:
    op.execute(_purge_function(PURGES_0028))
    op.drop_table("feature_request")
    op.execute("DROP FUNCTION IF EXISTS reject_opted_out_feature_request()")
