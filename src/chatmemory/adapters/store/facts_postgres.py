"""Postgres implementation of the personal-facts store.

Binds the statements in `facts_sql`. Rows are rebuilt through `PersonalFact`,
so a value written into the table by some path that skipped validation fails
loudly on read instead of reaching a prompt.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import facts_sql
from chatmemory.adapters.store.memory_postgres import _person_id
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.facts import (
    MULTI_VALUED_KINDS,
    FactKind,
    FactStore,
    PersonalFact,
    PersonalFacts,
    StoredFact,
)


def _requester(person: PersonRef) -> dict[str, object]:
    return {"platform": person.platform, "platform_user_id": person.platform_user_id}


class PostgresFactStore:
    """Implements `FactStore`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def set_fact(self, person: PersonRef, fact: PersonalFact) -> bool:
        async with self._engine.begin() as conn:
            # The same resolution memory uses, so a fact and a remembered turn
            # for one account land on one person -- and are purged together.
            person_id = await _person_id(conn, person)
            statement = (
                facts_sql.ADD_WALLET
                if fact.kind in MULTI_VALUED_KINDS
                else facts_sql.UPSERT_FACT
            )
            result = await conn.execute(
                statement,
                {"person_id": person_id, "kind": fact.kind.value, "value": fact.value},
            )
            return result.scalar() is not None

    async def facts_of(self, viewer: Viewer) -> PersonalFacts:
        async with self._engine.connect() as conn:
            rows = await conn.execute(facts_sql.FACTS_OF_REQUESTER, _requester(viewer.person))
            facts = tuple(
                StoredFact(
                    fact=PersonalFact(FactKind(str(row["kind"])), str(row["value"])),
                    updated_at=cast(datetime, row["updated_at"]),
                )
                for row in rows.mappings()
            )
        return PersonalFacts(facts)

    async def forget_fact(
        self, person: PersonRef, kind: FactKind, value: str | None = None
    ) -> bool:
        params = {**_requester(person), "kind": kind.value}
        statement = facts_sql.FORGET_FACT
        if value is not None:
            statement, params["value"] = facts_sql.FORGET_FACT_VALUE, value
        async with self._engine.begin() as conn:
            result = await conn.execute(statement, params)
        return bool(result.rowcount)

    async def forget_all_facts(self, person: PersonRef) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(facts_sql.FORGET_ALL_FACTS, _requester(person))
        return result.rowcount or 0


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresFactStore) -> FactStore:
        return store
