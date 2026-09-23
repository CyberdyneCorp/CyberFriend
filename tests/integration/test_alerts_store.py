"""The position-alert table: the cap, duplicates, claiming, and what removes a row.

The cap and the duplicate check are predicates on the insert, and claiming
advances each alert in the statement that returns it, so both are tested
against Postgres rather than a fake that would re-implement them. Erasure is
triggers, so it is tested by doing what a person does -- forgetting the saved
wallet, opting out, being deleted -- and reading the table.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.alerts_postgres import PostgresAlertStore
from chatmemory.adapters.store.facts_postgres import PostgresFactStore
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.alerts import (
    AddressSource,
    AlertKind,
    AlertLanguage,
    AlertRefusal,
    AlertState,
    AlertUpdate,
    LpProtocol,
    LpTarget,
    NewAlert,
    PositionAlert,
)
from chatmemory.ports.facts import FactKind, PersonalFact
from tests.integration.test_scheduled_tasks_store import seed_person

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LEO = PersonRef("discord", 7)
BRUNO = PersonRef("discord", 9)
WALLET = "0xdd8a0000000000000000000000000000000063d6"
TYPED = "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0"


def health(threshold: str = "1.3", **changes: object) -> NewAlert:
    alert = NewAlert(
        person=LEO,
        kind=AlertKind.AAVE_HEALTH,
        chain="base",
        address=WALLET,
        address_source=AddressSource.SAVED,
        language=AlertLanguage.PORTUGUESE,
        threshold=Decimal(threshold),
        state=AlertState.OK,
        last_value=Decimal("1.4268"),
    )
    return replace(alert, **changes)  # type: ignore[arg-type]


def lp(token_id: int, **changes: object) -> NewAlert:
    target = LpTarget(
        LpProtocol.UNISWAP_V3,
        token_id,
        "0xd0b53d9277642d899df5c87a3966a349a798f224",
        "WETH",
        "USDC",
        18,
        6,
        500,
    )
    alert = NewAlert(
        person=LEO,
        kind=AlertKind.LP_RANGE,
        chain="base",
        address=WALLET,
        address_source=AddressSource.SAVED,
        language=AlertLanguage.ENGLISH,
        lp=target,
        state=AlertState.IN_RANGE,
        last_value=Decimal(-197404),
    )
    return replace(alert, **changes)  # type: ignore[arg-type]


async def count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text("SELECT count(*) FROM position_alert")) or 0)


async def created(store: PostgresAlertStore, alert: NewAlert) -> PositionAlert:
    stored = await store.create(alert, NOW)
    assert isinstance(stored, PositionAlert), stored
    return stored


# --- creating -------------------------------------------------------------------


async def test_an_alert_round_trips_with_its_target_and_baseline(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)

    stored = await created(store, lp(4558452))

    assert stored.person == LEO
    assert stored.lp is not None and stored.lp.token_id == 4558452
    assert stored.lp.protocol is LpProtocol.UNISWAP_V3
    assert stored.state is AlertState.IN_RANGE
    assert stored.last_value == Decimal(-197404)
    assert stored.next_check_at == NOW
    assert stored.active
    [listed] = await store.for_person(LEO)
    assert listed == stored


async def test_the_cap_is_enforced_in_the_insert(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean, cap=10)
    for token_id in range(10):
        await created(store, lp(token_id))

    refused = await store.create(lp(99), NOW)

    assert refused is AlertRefusal.AT_CAP
    assert await count(clean) == 10


async def test_a_stopped_alert_does_not_use_up_a_slot(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean, cap=1)
    first = await created(store, lp(1))
    await store.record(
        first.id,
        AlertUpdate(AlertState.CLOSED, NOW, None, 0, None, fired=True, disable_reason="closed"),
        NOW,
    )

    assert isinstance(await store.create(lp(2), NOW), PositionAlert)


async def test_the_same_watch_twice_is_refused_as_a_duplicate(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    await created(store, health("1.3"))

    assert await store.create(health("1.3"), NOW) is AlertRefusal.DUPLICATE
    assert isinstance(await store.create(health("1.5"), NOW), PositionAlert)
    assert await count(clean) == 2


async def test_somebody_never_seen_gets_no_alert(clean: AsyncEngine) -> None:
    store = PostgresAlertStore(clean)

    assert await store.create(health(), NOW) is AlertRefusal.UNKNOWN_PERSON


@pytest.mark.parametrize(
    ("column", "value"),
    [("threshold", "1.00"), ("threshold", "5.5"), ("address", "0xABC"), ("chain", "solana")],
)
async def test_the_table_refuses_what_the_service_refuses(
    clean: AsyncEngine, column: str, value: str
) -> None:
    person_id = await seed_person(clean, LEO, "Leo")
    values = {
        "threshold": "1.3", "address": WALLET, "chain": "base", column: value,
    }
    with pytest.raises(IntegrityError):
        async with clean.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO position_alert (person_id, kind, chain, address, "
                    "address_source, threshold, language, next_check_at) VALUES "
                    "(:p, 'aave_health', :chain, :address, 'typed', "
                    "CAST(:threshold AS numeric), 'en', now())"
                ),
                {"p": person_id, **values},
            )


async def test_a_range_alert_must_carry_its_position(clean: AsyncEngine) -> None:
    person_id = await seed_person(clean, LEO, "Leo")
    with pytest.raises(IntegrityError):
        async with clean.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO position_alert (person_id, kind, chain, address, "
                    "address_source, language, next_check_at) VALUES "
                    "(:p, 'lp_range', 'base', :a, 'typed', 'en', now())"
                ),
                {"p": person_id, "a": WALLET},
            )


# --- only your own --------------------------------------------------------------


async def test_listing_and_deleting_are_by_owner(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    await seed_person(clean, BRUNO, "Bruno")
    store = PostgresAlertStore(clean)
    mine = await created(store, health())

    assert await store.for_person(BRUNO) == []
    assert await store.delete(BRUNO, mine.id) is False
    assert await store.delete(BRUNO, 999_999) is False
    assert await store.delete(LEO, mine.id) is True
    assert await count(clean) == 0


# --- claiming -------------------------------------------------------------------


async def test_a_due_alert_is_claimed_once_and_advanced_by_the_sweep(
    clean: AsyncEngine,
) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    stored = await created(store, health())

    first = await store.claim_due(NOW, 10, 300)
    second = await store.claim_due(NOW, 10, 300)
    later = await store.claim_due(NOW + timedelta(seconds=300), 10, 300)

    assert [a.id for a in first] == [stored.id]
    assert first[0].person == LEO
    assert first[0].threshold == Decimal("1.300")
    assert second == []
    assert [a.id for a in later] == [stored.id]


async def test_a_disabled_alert_is_never_claimed(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    await created(store, health())

    stopped = await store.disable(LEO, "direct messages are closed", NOW)

    assert stopped == 1
    assert await store.claim_due(NOW + timedelta(days=1), 10, 300) == []
    [listed] = await store.for_person(LEO)
    assert listed.disabled_reason == "direct messages are closed"


async def test_recording_a_sweep(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    stored = await created(store, lp(1))
    later = NOW + timedelta(minutes=5)

    await store.record(
        stored.id,
        AlertUpdate(AlertState.IN_RANGE, NOW, AlertState.OUT_OF_RANGE, 1, Decimal(-195000)),
        later,
    )
    failure = AlertUpdate(
        AlertState.IN_RANGE, NOW, AlertState.OUT_OF_RANGE, 1, Decimal(-195000), failed=True
    )
    await store.record(stored.id, failure, later)
    [pending] = await store.for_person(LEO)
    await store.record(
        stored.id,
        AlertUpdate(AlertState.OUT_OF_RANGE, later, None, 0, Decimal(-195000), fired=True),
        later,
    )
    [fired] = await store.for_person(LEO)

    assert pending.pending_state is AlertState.OUT_OF_RANGE and pending.pending_count == 1
    assert pending.consecutive_failures == 1
    assert pending.last_checked_at == later
    assert fired.state is AlertState.OUT_OF_RANGE and fired.state_since == later
    assert fired.last_fired_at == later
    assert fired.consecutive_failures == 0, "a good read resets the failure count"


# --- what removes an alert ------------------------------------------------------


async def test_deleting_the_person_deletes_their_alerts(clean: AsyncEngine) -> None:
    person_id = await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    await created(store, health())

    async with clean.begin() as conn:
        await conn.execute(
            text("DELETE FROM person_platform_id WHERE person_id = :i"), {"i": person_id}
        )
        await conn.execute(text("DELETE FROM person WHERE id = :i"), {"i": person_id})

    assert await count(clean) == 0


async def test_opting_out_deletes_every_alert_and_refuses_new_ones(clean: AsyncEngine) -> None:
    person_id = await seed_person(clean, LEO, "Leo")
    store = PostgresAlertStore(clean)
    await created(store, health())
    await created(store, lp(1, address=TYPED, address_source=AddressSource.TYPED))

    async with clean.begin() as conn:
        await conn.execute(
            text("INSERT INTO person_opt_out (person_id) VALUES (:i)"), {"i": person_id}
        )

    assert await count(clean) == 0
    assert not isinstance(await store.create(health("2.0"), NOW), PositionAlert)
    assert await count(clean) == 0


async def test_forgetting_the_saved_wallet_deletes_the_alerts_that_watch_it(
    clean: AsyncEngine,
) -> None:
    await seed_person(clean, LEO, "Leo")
    facts = PostgresFactStore(clean)
    await facts.set_fact(LEO, PersonalFact(FactKind.ETH_WALLET, WALLET))
    store = PostgresAlertStore(clean)
    await created(store, health())
    await created(store, lp(1))
    typed = await created(store, lp(2, address=TYPED, address_source=AddressSource.TYPED))

    assert await facts.forget_fact(LEO, FactKind.ETH_WALLET)

    assert [a.id for a in await store.for_person(LEO)] == [typed.id]


async def test_saving_a_different_wallet_forgets_the_old_ones_alerts(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    facts = PostgresFactStore(clean)
    await facts.set_fact(LEO, PersonalFact(FactKind.ETH_WALLET, WALLET))
    store = PostgresAlertStore(clean)
    await created(store, health())

    await facts.set_fact(LEO, PersonalFact(FactKind.ETH_WALLET, WALLET))
    assert await count(clean) == 1, "saving the same wallet again changes nothing"
    await facts.set_fact(LEO, PersonalFact(FactKind.ETH_WALLET, TYPED))

    assert await count(clean) == 0


async def test_forgetting_everything_deletes_the_saved_wallets_alerts(clean: AsyncEngine) -> None:
    await seed_person(clean, BRUNO, "Bruno")
    await seed_person(clean, LEO, "Leo")
    facts = PostgresFactStore(clean)
    await facts.set_fact(LEO, PersonalFact(FactKind.ETH_WALLET, WALLET))
    await facts.set_fact(BRUNO, PersonalFact(FactKind.ETH_WALLET, WALLET))
    store = PostgresAlertStore(clean)
    await created(store, health())
    theirs = await created(store, health(person=BRUNO))

    await facts.forget_all_facts(LEO)

    assert await store.for_person(LEO) == []
    assert [a.id for a in await store.for_person(BRUNO)] == [theirs.id]
