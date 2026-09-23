"""Position alerts: a condition on somebody's position, and its last known state.

A new table rather than more columns on `scheduled_task`: that one is a
question replayed through the model every one to twenty-four hours, and keeps
nothing about what the previous run found. An alert is the opposite -- no
model, and the previous state is the whole point, because it messages on a
change and never on a repeat.

Revision ID: 0020
Revises: 0019
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

KINDS = ("lp_range", "aave_health")
CHAINS = ("ethereum", "base", "arbitrum")
PROTOCOLS = ("uniswap_v3", "uniswap_v4")
LANGUAGES = ("en", "pt")
SOURCES = ("saved", "typed")
STATES = ("unknown", "in_range", "out_of_range", "closed", "ok", "below", "no_debt")

MIN_THRESHOLD = "1.05"
MAX_THRESHOLD = "5.0"


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# The same exclusion 0013-0015 apply one table over: nothing is stored for
# somebody who opted out, whatever path writes it.
GUARD = """
CREATE OR REPLACE FUNCTION reject_opted_out_position_alert() RETURNS trigger AS $$
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

# Opting out removes every alert in the same transaction as the flag, as the
# other purges do: an alert that outlived it would keep reading the person's
# wallet and messaging them on a timer.
PURGE_ON_OPT_OUT = """
CREATE OR REPLACE FUNCTION purge_position_alerts_on_opt_out() RETURNS trigger AS $$
BEGIN
    DELETE FROM position_alert WHERE person_id = NEW.person_id;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

# Forgetting the saved wallet -- `/forget`, "forget everything", or saving a
# different one -- removes the alerts that watch it on the strength of it
# being saved. In the database rather than in the forget path, so no way of
# removing the fact can leave the wallet watched. An alert on an address the
# person typed is theirs by that act and stays.
PURGE_ON_WALLET_FORGOTTEN = """
CREATE OR REPLACE FUNCTION purge_position_alerts_on_wallet_forgotten() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.value = OLD.value THEN
        RETURN NULL;
    END IF;
    DELETE FROM position_alert
     WHERE person_id = OLD.person_id
       AND address_source = 'saved'
       AND address = lower(OLD.value);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "position_alert",
        sa.Column("id", sa.BigInteger, primary_key=True),
        # Cascading is "removing a person removes their alerts", as for
        # scheduled tasks.
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("chain", sa.Text, nullable=False),
        # Checked once, when the alert was made, and stored lowercase. A sweep
        # has no asker to root it in, so this column *is* the clearance.
        sa.Column("address", sa.Text, nullable=False),
        sa.Column("address_source", sa.Text, nullable=False),
        # The pinned position (LP only). `pool_ref` is the v3 pool address or
        # the v4 pool id, and the token facts are what the message needs, so a
        # sweep never discovers anything.
        sa.Column("protocol", sa.Text, nullable=True),
        sa.Column("token_id", sa.Numeric(78, 0), nullable=True),
        sa.Column("pool_ref", sa.Text, nullable=True),
        sa.Column("token0_symbol", sa.Text, nullable=True),
        sa.Column("token1_symbol", sa.Text, nullable=True),
        sa.Column("token0_decimals", sa.SmallInteger, nullable=True),
        sa.Column("token1_decimals", sa.SmallInteger, nullable=True),
        sa.Column("fee", sa.Integer, nullable=True),
        # Health factor limit (Aave only). Bounded here as well as in the
        # service, for the reason 0017 bounds an interval.
        sa.Column("threshold", sa.Numeric(6, 3), nullable=True),
        sa.Column("notify_return", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("language", sa.Text, nullable=False),
        # The edge trigger: what the last reading said, since when, and a
        # change seen but not yet confirmed.
        sa.Column("state", sa.Text, nullable=False, server_default="unknown"),
        sa.Column(
            "state_since", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("pending_state", sa.Text, nullable=True),
        sa.Column("pending_count", sa.SmallInteger, nullable=False, server_default="0"),
        # The tick, or the health factor. NULL for "no debt".
        sa.Column("last_value", sa.Numeric, nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        # An alert is enabled while this is NULL. Kept rather than deleted
        # when it stops, as in 0017: "why did this stop?" is the question.
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.Text, nullable=True),
        sa.CheckConstraint(_one_of("kind", KINDS), name="ck_position_alert_kind"),
        sa.CheckConstraint(_one_of("chain", CHAINS), name="ck_position_alert_chain"),
        sa.CheckConstraint("address ~ '^0x[0-9a-f]{40}$'", name="ck_position_alert_address"),
        sa.CheckConstraint(
            _one_of("address_source", SOURCES), name="ck_position_alert_address_source"
        ),
        sa.CheckConstraint(_one_of("language", LANGUAGES), name="ck_position_alert_language"),
        sa.CheckConstraint(_one_of("state", STATES), name="ck_position_alert_state"),
        sa.CheckConstraint(
            "pending_state IS NULL OR " + _one_of("pending_state", STATES),
            name="ck_position_alert_pending_state",
        ),
        sa.CheckConstraint(
            f"threshold IS NULL OR threshold BETWEEN {MIN_THRESHOLD} AND {MAX_THRESHOLD}",
            name="ck_position_alert_threshold",
        ),
        # Each kind carries exactly the columns it reads, so a half-described
        # target cannot be stored and then read as something else.
        sa.CheckConstraint(
            "(kind = 'lp_range' AND " + _one_of("protocol", PROTOCOLS)
            + " AND token_id IS NOT NULL AND pool_ref IS NOT NULL"
            " AND token0_symbol IS NOT NULL AND token1_symbol IS NOT NULL"
            " AND token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL"
            " AND fee IS NOT NULL AND threshold IS NULL)"
            " OR (kind = 'aave_health' AND threshold IS NOT NULL"
            " AND protocol IS NULL AND token_id IS NULL AND pool_ref IS NULL)",
            name="ck_position_alert_target",
        ),
    )
    # The sweep's only scan, partial so it stays the size of what is live.
    op.create_index(
        "ix_position_alert_due",
        "position_alert",
        ["next_check_at"],
        postgresql_where=sa.text("disabled_at IS NULL"),
    )
    # Listing and the per-person cap both read by owner.
    op.create_index("ix_position_alert_person", "position_alert", ["person_id"])
    # The same watch twice is one watch: two alerts would send two messages
    # for one change. `ON CONFLICT` in the insert names this index.
    op.execute(
        "CREATE UNIQUE INDEX ux_position_alert_target ON position_alert "
        "(person_id, kind, chain, address, (coalesce(token_id, -1)), "
        "(coalesce(threshold, 0))) WHERE disabled_at IS NULL"
    )

    op.execute(GUARD)
    op.execute(
        "CREATE TRIGGER trg_position_alert_opt_out "
        "BEFORE INSERT OR UPDATE ON position_alert "
        "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_position_alert()"
    )
    op.execute(PURGE_ON_OPT_OUT)
    op.execute(
        "CREATE TRIGGER trg_person_opt_out_purges_position_alerts "
        "AFTER INSERT OR UPDATE ON person_opt_out "
        "FOR EACH ROW EXECUTE FUNCTION purge_position_alerts_on_opt_out()"
    )
    op.execute(PURGE_ON_WALLET_FORGOTTEN)
    op.execute(
        "CREATE TRIGGER trg_person_fact_wallet_purges_position_alerts "
        "AFTER DELETE OR UPDATE OF value ON person_fact "
        "FOR EACH ROW WHEN (OLD.kind = 'eth_wallet') "
        "EXECUTE FUNCTION purge_position_alerts_on_wallet_forgotten()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_person_fact_wallet_purges_position_alerts ON person_fact"
    )
    op.execute("DROP FUNCTION IF EXISTS purge_position_alerts_on_wallet_forgotten()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_person_opt_out_purges_position_alerts ON person_opt_out"
    )
    op.execute("DROP FUNCTION IF EXISTS purge_position_alerts_on_opt_out()")
    op.drop_table("position_alert")
    op.execute("DROP FUNCTION IF EXISTS reject_opted_out_position_alert()")
