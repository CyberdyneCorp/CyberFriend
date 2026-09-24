"""Several wallets, a home address and a birth date, against a real database.

Migration 0022 splits the table's uniqueness in two partial indexes and the
upserts name each one, so what "saving again" means for each kind is tested
against Postgres: a wallet adds, anything else replaces. The downgrade is run
too, because it has to choose which of several wallets survives.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.facts_postgres import PostgresFactStore
from chatmemory.app.facts import FactOutcome, PersonalFactsService
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.facts import (
    MAX_WALLETS_PER_KIND,
    FactKind,
    FactRejection,
    PersonalFact,
)
from tests.integration.test_alert_kinds_store import alembic

pytestmark = pytest.mark.asyncio

LEO = PersonRef("discord", 2001)
ANA = PersonRef("discord", 2002)
WALLETS = tuple(f"0x{i:040x}" for i in range(1, MAX_WALLETS_PER_KIND + 2))


def viewer(person: PersonRef) -> Viewer:
    return Viewer(person=person, visible_channels=frozenset({ChannelRef("discord", 1)}))


async def rows(engine: AsyncEngine, kind: str) -> list[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT value FROM person_fact WHERE kind = :kind ORDER BY id"), {"kind": kind}
        )
        return [str(v) for (v,) in result]


async def test_saving_a_second_wallet_adds_it(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    assert await store.set_fact(LEO, PersonalFact(FactKind.ETH_WALLET, WALLETS[0]))
    assert await store.set_fact(LEO, PersonalFact(FactKind.ETH_WALLET, WALLETS[1]))
    # Idempotent: the same address again is still one row.
    assert await store.set_fact(LEO, PersonalFact(FactKind.ETH_WALLET, WALLETS[0]))

    facts = await store.facts_of(viewer(LEO))
    assert facts.values(FactKind.ETH_WALLET) == (WALLETS[1], WALLETS[0])
    assert await rows(clean, "eth_wallet") == [WALLETS[0], WALLETS[1]]


async def test_every_other_kind_is_still_replaced(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await store.set_fact(LEO, PersonalFact(FactKind.HOME_ADDRESS, "Vargem Grande"))
    await store.set_fact(LEO, PersonalFact(FactKind.HOME_ADDRESS, "Rio de Janeiro"))
    await store.set_fact(LEO, PersonalFact(FactKind.BIRTH_DATE, "21/06/1981"))

    facts = await store.facts_of(viewer(LEO))
    assert facts.values(FactKind.HOME_ADDRESS) == ("Rio de Janeiro",)
    assert facts.get(FactKind.BIRTH_DATE) == "1981-06-21"


async def test_the_table_refuses_a_second_row_of_a_single_kind(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await store.set_fact(LEO, PersonalFact(FactKind.EMAIL, "leo@example.com"))
    async with clean.connect() as conn:
        person_id = await conn.scalar(text("SELECT person_id FROM person_fact LIMIT 1"))
    with pytest.raises(IntegrityError):
        async with clean.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO person_fact (person_id, kind, value) "
                    "VALUES (:p, 'email', 'x@y.z')"
                ),
                {"p": person_id},
            )


async def test_the_cap_refuses_a_sixth_wallet_but_not_a_repeat(clean: AsyncEngine) -> None:
    service = PersonalFactsService(PostgresFactStore(clean))
    for wallet in WALLETS[:MAX_WALLETS_PER_KIND]:
        result = await service.remember(viewer(LEO), FactKind.ETH_WALLET, wallet)
        assert result.outcome is FactOutcome.STORED

    refused = await service.remember(viewer(LEO), FactKind.ETH_WALLET, WALLETS[-1])
    again = await service.remember(viewer(LEO), FactKind.ETH_WALLET, WALLETS[0])

    assert refused.outcome is FactOutcome.REJECTED
    assert refused.rejection is FactRejection.TOO_MANY
    assert again.outcome is FactOutcome.STORED
    assert len(await rows(clean, "eth_wallet")) == MAX_WALLETS_PER_KIND


async def test_forgetting_one_wallet_keeps_the_others(clean: AsyncEngine) -> None:
    service = PersonalFactsService(PostgresFactStore(clean))
    for wallet in WALLETS[:3]:
        await service.remember(viewer(LEO), FactKind.ETH_WALLET, wallet)
    await service.remember(viewer(ANA), FactKind.ETH_WALLET, WALLETS[1])

    # Named in any case: the stored form is lowercase.
    shouted = "0x" + WALLETS[1][2:].upper()
    assert await service.forget(viewer(LEO), FactKind.ETH_WALLET, shouted)

    store = PostgresFactStore(clean)
    leo = await store.facts_of(viewer(LEO))
    assert leo.values(FactKind.ETH_WALLET) == (WALLETS[0], WALLETS[2])
    # The same address saved by somebody else is theirs, and stays.
    ana = await store.facts_of(viewer(ANA))
    assert ana.values(FactKind.ETH_WALLET) == (WALLETS[1],)


async def test_forgetting_the_wallet_kind_forgets_every_wallet(clean: AsyncEngine) -> None:
    service = PersonalFactsService(PostgresFactStore(clean))
    for wallet in WALLETS[:3]:
        await service.remember(viewer(LEO), FactKind.ETH_WALLET, wallet)
    await service.remember(viewer(LEO), FactKind.PHONE, "+5521980703795")

    assert await service.forget(viewer(LEO), FactKind.ETH_WALLET)

    facts = await PostgresFactStore(clean).facts_of(viewer(LEO))
    assert facts.values(FactKind.ETH_WALLET) == ()
    assert facts.get(FactKind.PHONE) == "+5521980703795"


async def test_downgrading_keeps_the_newest_wallet_and_drops_the_new_kinds(
    clean: AsyncEngine,
) -> None:
    store = PostgresFactStore(clean)
    for wallet in WALLETS[:3]:
        await store.set_fact(LEO, PersonalFact(FactKind.ETH_WALLET, wallet))
    await store.set_fact(ANA, PersonalFact(FactKind.ETH_WALLET, WALLETS[4]))
    await store.set_fact(LEO, PersonalFact(FactKind.HOME_ADDRESS, "Vargem Grande"))
    await store.set_fact(LEO, PersonalFact(FactKind.BIRTH_DATE, "1981-06-21"))
    await store.set_fact(LEO, PersonalFact(FactKind.PREFERRED_NAME, "Leo"))
    await clean.dispose()

    try:
        alembic("downgrade", "0021")
        async with clean.connect() as conn:
            kept = (
                await conn.execute(text("SELECT kind, value FROM person_fact ORDER BY id"))
            ).all()
        assert [(k, v) for k, v in kept] == [
            ("eth_wallet", WALLETS[2]),
            ("eth_wallet", WALLETS[4]),
            ("preferred_name", "Leo"),
        ]
    finally:
        await clean.dispose()
        alembic("upgrade", "head")
    assert (await store.facts_of(viewer(LEO))).get(FactKind.PREFERRED_NAME) == "Leo"
