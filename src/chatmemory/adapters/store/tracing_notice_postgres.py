"""Implements `TracingNoticeStore` over the `person` row."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import tracing_notice_sql as sql
from chatmemory.adapters.store.memory_postgres import _person_id
from chatmemory.domain.identity import PersonRef


class PostgresTracingNoticeStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def claim_notice(self, person: PersonRef, version: int, now: datetime) -> bool:
        async with self._engine.begin() as conn:
            person_id = await _person_id(conn, person)
            claimed = await conn.execute(
                sql.CLAIM_NOTICE, {"person_id": person_id, "version": version, "now": now}
            )
            return claimed.first() is not None
