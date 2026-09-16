"""Conversation memory: turns and summaries, per person and per location.

Revision ID: 0013
Revises: 0011
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
# 0011 is the head, not 0012: the chain runs 0010 -> 0012 -> 0011 (see 0012).
down_revision = "0011"
branch_labels = None
depends_on = None

# The same exclusion as 0008, one table over, and for the same reason: the
# guarantee belongs to the database, not to every caller that might write a
# turn. A BEFORE INSERT trigger returning NULL drops the row silently -- an
# opted-out person's question is still answered, it is just not remembered,
# and an exception would turn "not remembered" into "not answered".
#
# UPDATE is covered as well so that no future statement can move a turn onto
# an opted-out person.
MEMORY_GUARD = """
CREATE OR REPLACE FUNCTION reject_opted_out_memory() RETURNS trigger AS $$
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

# The purge half, hung off the opt-out record itself. `OptOutService` writes
# `person_opt_out` first and purges second; a memory purge triggered by that
# write runs in the same transaction as the flag, so there is no moment at
# which the flag is recorded and the memory still readable -- and no path that
# records an opt-out (the service, an operator at a psql prompt, a future
# surface) can forget to purge memory. UPDATE fires on a re-recorded opt-out,
# which is harmless and repairs anything a half-applied earlier purge left.
MEMORY_PURGE_ON_OPT_OUT = """
CREATE OR REPLACE FUNCTION purge_memory_on_opt_out() RETURNS trigger AS $$
BEGIN
    DELETE FROM conversation_turn WHERE person_id = NEW.person_id;
    DELETE FROM conversation_summary WHERE person_id = NEW.person_id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

# A NULL element makes `<@` answer NULL rather than false for the whole array,
# which a WHERE clause treats as "not returned" -- the safe direction -- but a
# provenance list with a hole in it is unknown provenance, and is refused at
# the door rather than relied on to fail the right way later.
NO_NULL_CHANNELS = "array_position({column}, NULL) IS NULL"


def _location_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("location_platform", sa.Text, nullable=False),
        sa.Column("location_id", sa.BigInteger, nullable=False),
        # Part of the key: a DM answer was scoped to one person and a channel
        # answer to everyone present, so one must never inform the other.
        sa.Column("location_direct", sa.Boolean, nullable=False),
    ]


def _person_column() -> sa.Column[object]:
    # Canonical person, not platform account, and cascading: deleting a person
    # must not leave a record of what they asked behind.
    return sa.Column(
        "person_id",
        sa.BigInteger,
        sa.ForeignKey("person.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "conversation_turn",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _person_column(),
        *_location_columns(),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        # Platform channel ids, the same values a viewer's readable set binds
        # as `:channel_ids`. NOT NULL: a turn with unknown provenance is not
        # stored at all, so it can never be the turn that is replayed.
        sa.Column("channel_ids", postgresql.ARRAY(sa.BigInteger), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            NO_NULL_CHANNELS.format(column="channel_ids"),
            name="ck_conversation_turn_channels_known",
        ),
    )
    op.create_index(
        "ix_conversation_turn_key",
        "conversation_turn",
        ["person_id", "location_direct", "location_platform", "location_id", "id"],
    )
    op.create_index("ix_conversation_turn_created", "conversation_turn", ["created_at"])

    op.create_table(
        "conversation_summary",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _person_column(),
        *_location_columns(),
        sa.Column("text", sa.Text, nullable=False),
        # The union of everything the summary replaced. Checked as one unit:
        # if any is unreadable the whole summary is withheld.
        sa.Column(
            "covered_channel_ids", postgresql.ARRAY(sa.BigInteger), nullable=False
        ),
        sa.Column("through_turn_id", sa.BigInteger, nullable=False),
        # When the earliest content it carries was said. Retention keys on
        # this, not on `created_at`, for the reason windows key on `starts_at`:
        # a summary written today of last quarter's turns holds last quarter.
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            NO_NULL_CHANNELS.format(column="covered_channel_ids"),
            name="ck_conversation_summary_channels_known",
        ),
    )
    op.create_index(
        "ix_conversation_summary_key",
        "conversation_summary",
        ["person_id", "location_direct", "location_platform", "location_id"],
    )
    op.create_index("ix_conversation_summary_starts", "conversation_summary", ["starts_at"])

    op.execute(MEMORY_GUARD)
    for table in ("conversation_turn", "conversation_summary"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_opt_out "
            f"BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_memory()"
        )

    op.execute(MEMORY_PURGE_ON_OPT_OUT)
    op.execute(
        "CREATE TRIGGER trg_person_opt_out_purges_memory "
        "AFTER INSERT OR UPDATE ON person_opt_out "
        "FOR EACH ROW EXECUTE FUNCTION purge_memory_on_opt_out()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_person_opt_out_purges_memory ON person_opt_out")
    op.execute("DROP FUNCTION IF EXISTS purge_memory_on_opt_out()")
    op.drop_table("conversation_summary")
    op.drop_table("conversation_turn")
    op.execute("DROP FUNCTION IF EXISTS reject_opted_out_memory()")
