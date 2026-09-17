"""Personal facts: preferred name, email and preferred language, per person.

Revision ID: 0014
Revises: 0013
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# Mirrors `ports.facts.FactKind`. In the database as well as the enum so a
# statement written outside the service -- an operator at a psql prompt, a
# future surface -- still cannot open a free-form notes store.
FACT_KINDS = ("preferred_name", "email", "preferred_language")

# The widest value any kind allows (an email address, RFC 5321). The domain
# enforces the per-kind bounds; this only stops an unbounded blob arriving by
# some path that skipped it.
MAX_VALUE_CHARS = 254

# The same exclusion as 0008 and 0013, one table over. A trigger returning NULL
# drops the row silently: "call me Leo" from an opted-out person is not stored,
# and is not an error. Its own function rather than 0013's, so that neither
# migration's downgrade can remove the other's guard.
FACT_GUARD = """
CREATE OR REPLACE FUNCTION reject_opted_out_fact() RETURNS trigger AS $$
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

# Purged from the opt-out record itself, as 0013 does for memory: the flag and
# the purge share a transaction, and no path that records an opt-out can forget
# the facts half.
FACT_PURGE_ON_OPT_OUT = """
CREATE OR REPLACE FUNCTION purge_facts_on_opt_out() RETURNS trigger AS $$
BEGIN
    DELETE FROM person_fact WHERE person_id = NEW.person_id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    kinds = ", ".join(f"'{k}'" for k in FACT_KINDS)
    op.create_table(
        "person_fact",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # Canonical person, cascading: deleting a person must not leave their
        # email behind.
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(f"kind IN ({kinds})", name="ck_person_fact_kind"),
        sa.CheckConstraint(
            f"char_length(value) BETWEEN 1 AND {MAX_VALUE_CHARS}",
            name="ck_person_fact_value_length",
        ),
        # One of each: changing a fact replaces it, and the upsert keys on this.
        sa.UniqueConstraint("person_id", "kind", name="uq_person_fact_person_kind"),
    )

    op.execute(FACT_GUARD)
    op.execute(
        "CREATE TRIGGER trg_person_fact_opt_out "
        "BEFORE INSERT OR UPDATE ON person_fact "
        "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_fact()"
    )
    op.execute(FACT_PURGE_ON_OPT_OUT)
    op.execute(
        "CREATE TRIGGER trg_person_opt_out_purges_facts "
        "AFTER INSERT OR UPDATE ON person_opt_out "
        "FOR EACH ROW EXECUTE FUNCTION purge_facts_on_opt_out()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_person_opt_out_purges_facts ON person_opt_out")
    op.execute("DROP FUNCTION IF EXISTS purge_facts_on_opt_out()")
    op.drop_table("person_fact")
    op.execute("DROP FUNCTION IF EXISTS reject_opted_out_fact()")
