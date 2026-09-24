"""Two more alert kinds: a BTC/ETH price level, and "near the range edge".

A price alert watches no wallet, so for that kind alone `chain`, `address` and
`address_source` are NULL, and it carries `asset`, `direction` and
`price_level` instead. The per-kind check is rewritten so each kind still
carries exactly the columns it reads: the old one would read a price row as a
half-described Aave alert.

"Near the edge" is not a kind. It is an optional `edge_percent` on a range
alert, which adds one state (`near_edge`) between in range and out of range.

The unique index is rebuilt over the new columns, with the nullable ones
coalesced, because NULLs are distinct in a unique index and two identical
price alerts would otherwise both be stored and both message. The insert's
`ON CONFLICT` names the same expressions.

Revision ID: 0021
Revises: 0020
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

OLD_KINDS = ("lp_range", "aave_health")
KINDS = (*OLD_KINDS, "price")
CHAINS = ("ethereum", "base", "arbitrum")
PROTOCOLS = ("uniswap_v3", "uniswap_v4")
OLD_STATES = ("unknown", "in_range", "out_of_range", "closed", "ok", "below", "no_debt")
STATES = (*OLD_STATES, "near_edge", "above")
ASSETS = ("BTC", "ETH")
DIRECTIONS = ("above", "below")
MIN_EDGE, MAX_EDGE = "1", "50"

OLD_INDEX = (
    "CREATE UNIQUE INDEX ux_position_alert_target ON position_alert "
    "(person_id, kind, chain, address, (coalesce(token_id, -1)), "
    "(coalesce(threshold, 0))) WHERE disabled_at IS NULL"
)
INDEX = (
    "CREATE UNIQUE INDEX ux_position_alert_target ON position_alert "
    "(person_id, kind, (coalesce(chain, '')), (coalesce(address, '')), "
    "(coalesce(token_id, -1)), (coalesce(threshold, 0)), (coalesce(asset, '')), "
    "(coalesce(direction, '')), (coalesce(price_level, 0))) WHERE disabled_at IS NULL"
)

_LP_COLUMNS = (
    " AND token_id IS NOT NULL AND pool_ref IS NOT NULL"
    " AND token0_symbol IS NOT NULL AND token1_symbol IS NOT NULL"
    " AND token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL"
    " AND fee IS NOT NULL AND threshold IS NULL"
)
_NO_LP = " AND protocol IS NULL AND token_id IS NULL AND pool_ref IS NULL"
_WALLET = " AND chain IS NOT NULL AND address IS NOT NULL AND address_source IS NOT NULL"
_NO_PRICE = " AND asset IS NULL AND direction IS NULL AND price_level IS NULL"


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


OLD_TARGET = (
    "(kind = 'lp_range' AND " + _one_of("protocol", PROTOCOLS) + _LP_COLUMNS + ")"
    " OR (kind = 'aave_health' AND threshold IS NOT NULL" + _NO_LP + ")"
)
TARGET = (
    "(kind = 'lp_range' AND " + _one_of("protocol", PROTOCOLS) + _LP_COLUMNS
    + _WALLET + _NO_PRICE + ")"
    " OR (kind = 'aave_health' AND threshold IS NOT NULL" + _NO_LP + _WALLET + _NO_PRICE
    + " AND edge_percent IS NULL)"
    # A price alert has no wallet at all: nothing for the sweep to send to a
    # chain endpoint, and nothing for forgetting a wallet to match.
    " OR (kind = 'price' AND chain IS NULL AND address IS NULL AND address_source IS NULL"
    " AND " + _one_of("asset", ASSETS) + " AND " + _one_of("direction", DIRECTIONS)
    + " AND price_level IS NOT NULL AND threshold IS NULL AND edge_percent IS NULL"
    + _NO_LP + ")"
)


def _replace_check(name: str, condition: str) -> None:
    op.drop_constraint(name, "position_alert", type_="check")
    op.create_check_constraint(name, "position_alert", condition)


def upgrade() -> None:
    # The level in US dollars. Wide enough for BTC at any price anybody types,
    # and positive, since a price never reaches zero from above.
    op.add_column("position_alert", sa.Column("asset", sa.Text, nullable=True))
    op.add_column("position_alert", sa.Column("direction", sa.Text, nullable=True))
    op.add_column("position_alert", sa.Column("price_level", sa.Numeric(24, 8), nullable=True))
    # Percent of the price, bounded here as well as in the service.
    op.add_column("position_alert", sa.Column("edge_percent", sa.Numeric(4, 1), nullable=True))
    for column in ("chain", "address", "address_source"):
        op.alter_column("position_alert", column, nullable=True)

    _replace_check("ck_position_alert_kind", _one_of("kind", KINDS))
    _replace_check("ck_position_alert_state", _one_of("state", STATES))
    _replace_check(
        "ck_position_alert_pending_state",
        "pending_state IS NULL OR " + _one_of("pending_state", STATES),
    )
    op.create_check_constraint(
        "ck_position_alert_price",
        "position_alert",
        "price_level IS NULL OR price_level > 0",
    )
    op.create_check_constraint(
        "ck_position_alert_edge",
        "position_alert",
        f"edge_percent IS NULL OR edge_percent BETWEEN {MIN_EDGE} AND {MAX_EDGE}",
    )
    _replace_check("ck_position_alert_target", TARGET)
    op.execute("DROP INDEX ux_position_alert_target")
    op.execute(INDEX)


def downgrade() -> None:
    # Rows the old schema cannot describe go first: price alerts outright, and
    # the near-edge state folded back into "in range", which it is.
    op.execute("DELETE FROM position_alert WHERE kind = 'price'")
    op.execute("UPDATE position_alert SET state = 'in_range' WHERE state = 'near_edge'")
    op.execute(
        "UPDATE position_alert SET pending_state = NULL, pending_count = 0 "
        "WHERE pending_state IN ('near_edge', 'above')"
    )
    op.execute("DROP INDEX ux_position_alert_target")
    op.execute(OLD_INDEX)
    _replace_check("ck_position_alert_target", OLD_TARGET)
    op.drop_constraint("ck_position_alert_edge", "position_alert", type_="check")
    op.drop_constraint("ck_position_alert_price", "position_alert", type_="check")
    _replace_check(
        "ck_position_alert_pending_state",
        "pending_state IS NULL OR " + _one_of("pending_state", OLD_STATES),
    )
    _replace_check("ck_position_alert_state", _one_of("state", OLD_STATES))
    _replace_check("ck_position_alert_kind", _one_of("kind", OLD_KINDS))
    for column in ("chain", "address", "address_source"):
        op.alter_column("position_alert", column, nullable=False)
    for column in ("edge_percent", "price_level", "direction", "asset"):
        op.drop_column("position_alert", column)
