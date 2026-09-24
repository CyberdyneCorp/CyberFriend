"""Price and near-edge alerts in the table: no wallet, one watch, and the way back.

A price alert is the one row with no chain and no address, which the per-kind
check allows for that kind alone; the unique index coalesces those NULLs so the
same level twice is still one watch; a near-edge request on a watched position
adds its distance to that alert rather than making a second; and downgrading
past 0021 removes the rows the old schema cannot describe first.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.alerts_postgres import PostgresAlertStore
from chatmemory.ports.alerts import (
    AlertKind,
    AlertLanguage,
    AlertRefusal,
    AlertState,
    AlertUpdate,
    NewAlert,
    PositionAlert,
    PriceDirection,
    PriceTarget,
)
from tests.integration.conftest import DB_URL
from tests.integration.test_alerts_store import LEO, WALLET, count, created, health, lp
from tests.integration.test_scheduled_tasks_store import seed_person

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def price(asset: str = "BTC", level: str = "100000", **changes: Any) -> NewAlert:
    alert = NewAlert(
        person=LEO,
        kind=AlertKind.PRICE,
        chain=None,
        address=None,
        address_source=None,
        language=AlertLanguage.PORTUGUESE,
        price=PriceTarget(asset, PriceDirection.ABOVE, Decimal(level)),
        state=AlertState.BELOW,
        last_value=Decimal("97412.35"),
    )
    return replace(alert, **changes)


async def test_a_price_alert_round_trips_with_no_wallet(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)

    stored = await created(store, price())

    assert (stored.chain, stored.address, stored.address_source) == (None, None, None)
    assert stored.price == PriceTarget("BTC", PriceDirection.ABOVE, Decimal(100000))
    assert stored.state is AlertState.BELOW
    [listed] = await store.for_person(LEO)
    assert listed == stored
    [claimed] = await store.claim_due(NOW, 10, 300)
    assert claimed.price == stored.price
    await store.record(
        stored.id, AlertUpdate(AlertState.ABOVE, NOW, None, 0, Decimal("100412.35"), True), NOW
    )
    [after] = await store.for_person(LEO)
    assert after.state is AlertState.ABOVE and after.last_fired_at == NOW


async def test_the_same_level_twice_is_one_watch(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    await created(store, price())

    assert await store.create(price(), NOW) is AlertRefusal.DUPLICATE
    await created(store, price(level="110000"))
    await created(store, price(asset="ETH", level="2500"))
    down = price(price=PriceTarget("BTC", PriceDirection.BELOW, Decimal(100000)))
    await created(store, down)
    assert await count(clean) == 4


async def test_the_cap_counts_every_kind(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean, cap=3)
    await created(store, price())
    await created(store, health())
    await created(store, lp(1))

    assert await store.create(price(asset="ETH", level="2500"), NOW) is AlertRefusal.AT_CAP


async def test_a_near_edge_request_adds_its_distance_to_the_watched_position(
    clean: AsyncEngine,
) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    plain = await created(store, lp(210171))

    upgraded = await created(
        store, lp(210171, edge_percent=Decimal(3), state=AlertState.NEAR_EDGE)
    )

    assert upgraded.id == plain.id and upgraded.edge_percent == Decimal(3)
    assert upgraded.state is AlertState.NEAR_EDGE
    assert await count(clean) == 1
    assert await store.create(lp(210171, edge_percent=Decimal(3)), NOW) is AlertRefusal.DUPLICATE
    assert await store.create(lp(210171), NOW) is AlertRefusal.DUPLICATE


@pytest.mark.parametrize(
    ("columns", "values"),
    [
        # A price alert with a wallet.
        ("chain, address, address_source, asset, direction, price_level",
         "'base', :w, 'typed', 'BTC', 'above', 100000"),
        # Outside the closed vocabulary.
        ("asset, direction, price_level", "'SOL', 'above', 200"),
        ("asset, direction, price_level", "'BTC', 'sideways', 100000"),
        ("asset, direction, price_level", "'BTC', 'above', 0"),
        ("asset, direction", "'BTC', 'above'"),
    ],
)
async def test_the_table_refuses_a_half_described_price_alert(
    clean: AsyncEngine, columns: str, values: str
) -> None:
    person_id = await seed_person(clean, LEO, "Leo")
    with pytest.raises(IntegrityError):
        async with clean.begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO position_alert (person_id, kind, {columns}, language, "
                    f"next_check_at) VALUES (:p, 'price', {values}, 'en', now())"
                ),
                {"p": person_id, "w": WALLET},
            )


@pytest.mark.parametrize("edge", ["0.5", "50.5"])
async def test_the_table_bounds_the_edge_distance(clean: AsyncEngine, edge: str) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    stored = await created(store, lp(1))
    with pytest.raises(IntegrityError):
        async with clean.begin() as conn:
            await conn.execute(
                text("UPDATE position_alert SET edge_percent = CAST(:e AS numeric) WHERE id = :i"),
                {"e": edge, "i": stored.id},
            )


def alembic(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": DB_URL},
        check=True,
        capture_output=True,
    )


async def test_downgrading_removes_price_rows_first_and_keeps_the_rest(
    clean: AsyncEngine,
) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    await created(store, price())
    kept = await created(store, lp(210171, edge_percent=Decimal(3), state=AlertState.NEAR_EDGE))
    await created(store, health())
    await clean.dispose()

    try:
        alembic("downgrade", "0020")
        async with clean.connect() as conn:
            rows = (
                await conn.execute(text("SELECT id, kind, state FROM position_alert ORDER BY id"))
            ).all()
        states = [(r.kind, r.state) for r in rows]
        assert states == [("lp_range", "in_range"), ("aave_health", "ok")]
        assert rows[0].id == kept.id
    finally:
        await clean.dispose()
        alembic("upgrade", "head")
    assert isinstance((await store.for_person(LEO))[0], PositionAlert)
