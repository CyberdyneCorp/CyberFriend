"""The preferred currency against a real database.

Migration 0027 widens the kind check; nothing else about the table changes, so
the currency is one row per person like the language, replaced when stated
again, and the downgrade drops it without touching the other kinds.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.facts_postgres import PostgresFactStore
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.facts import FactKind, PersonalFact
from tests.integration.test_alert_kinds_store import alembic

pytestmark = pytest.mark.asyncio

LEO = PersonRef("discord", 2101)


def viewer(person: PersonRef) -> Viewer:
    return Viewer(person=person, visible_channels=frozenset({ChannelRef("discord", 1)}))


async def test_the_currency_is_stored_as_its_code_and_replaced(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    assert await store.set_fact(LEO, PersonalFact(FactKind.PREFERRED_CURRENCY, "reais"))
    assert await store.set_fact(LEO, PersonalFact(FactKind.PREFERRED_CURRENCY, "euros"))

    facts = await store.facts_of(viewer(LEO))
    assert facts.values(FactKind.PREFERRED_CURRENCY) == ("EUR",)
    assert await store.forget_fact(LEO, FactKind.PREFERRED_CURRENCY)
    assert (await store.facts_of(viewer(LEO))).get(FactKind.PREFERRED_CURRENCY) is None


async def test_downgrading_drops_only_the_currency(clean: AsyncEngine) -> None:
    store = PostgresFactStore(clean)
    await store.set_fact(LEO, PersonalFact(FactKind.PREFERRED_CURRENCY, "BRL"))
    await store.set_fact(LEO, PersonalFact(FactKind.PREFERRED_NAME, "Leo"))
    await clean.dispose()

    try:
        alembic("downgrade", "0026")
        async with clean.connect() as conn:
            kept = (await conn.execute(text("SELECT kind, value FROM person_fact"))).all()
        assert [(k, v) for k, v in kept] == [("preferred_name", "Leo")]
    finally:
        await clean.dispose()
        alembic("upgrade", "head")
