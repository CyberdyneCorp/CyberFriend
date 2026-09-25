"""Feature requests: what people asked the assistant to learn to do.

One row per suggestion, holding the person's own words and nothing about the
conversation around them: no message text, no channel name, only the ids a
console can resolve itself.

Privacy follows the pattern of 0013 and 0014. A suggestion from an opted-out
person is dropped before it is stored, and recording an opt-out deletes every
suggestion the person made, in the same transaction as the flag. Deleting the
person cascades. When the shared `purge_person_derived` function lands
(0028 on fix/opt-out-leftovers), this revision must be re-chained onto it,
its DELETE added to that function, and the purge trigger here dropped:
self-service erasure calls the function directly, not the opt-out trigger,
so until then erasure without opt-out relies on the person-row cascade.

Revision ID: 0030
Revises: 0027
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0027"
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

PURGE_ON_OPT_OUT = """
CREATE OR REPLACE FUNCTION purge_feature_requests_on_opt_out() RETURNS trigger AS $$
BEGIN
    DELETE FROM feature_request WHERE person_id = NEW.person_id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""


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
    op.execute(PURGE_ON_OPT_OUT)
    op.execute(
        "CREATE TRIGGER trg_person_opt_out_purges_feature_requests "
        "AFTER INSERT OR UPDATE ON person_opt_out "
        "FOR EACH ROW EXECUTE FUNCTION purge_feature_requests_on_opt_out()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_person_opt_out_purges_feature_requests ON person_opt_out"
    )
    op.execute("DROP FUNCTION IF EXISTS purge_feature_requests_on_opt_out()")
    op.drop_table("feature_request")
    op.execute("DROP FUNCTION IF EXISTS reject_opted_out_feature_request()")
