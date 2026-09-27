"""Implements `UsageDirectory` over our own tables."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import usage_sql as sql
from chatmemory.app.usage import platform_id
from chatmemory.ports.usage import UsageExclusions, UsageWindow, ViewedPerson, VoiceUsage

DELETION_LAG = timedelta(days=1)
"""How long after Langfuse accepted a deletion its trace is still treated as present."""


class PostgresUsageDirectory:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def exclusions(self, window: UsageWindow) -> UsageExclusions:
        async with self._engine.connect() as conn:
            people: Sequence[str] = (await conn.execute(sql.EXCLUDED_PEOPLE)).scalars().all()
            erased = (await conn.execute(sql.ERASED_PEOPLE)).all()
            pending: Sequence[str] = (
                await conn.execute(
                    sql.PENDING_TRACES,
                    {"confirmed_after": datetime.now(UTC) - DELETION_LAG},
                )
            ).scalars().all()
        return UsageExclusions(
            people=frozenset(people),
            erased_before={row.user_id: row.erased_before for row in erased},
            trace_ids=frozenset(pending),
        )

    async def names(self, user_ids: Sequence[str]) -> Mapping[str, str]:
        ids = sorted({int(u) for u in user_ids if platform_id(u)})
        if not ids:
            return {}
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sql.NAMES, {"ids": ids})).all()
        return {row.user_id: row.display_name for row in rows}

    async def person(self, user_id: str) -> ViewedPerson | None:
        if platform_id(user_id) is None:
            return None
        async with self._engine.connect() as conn:
            row = (await conn.execute(sql.PERSON, {"user_id": int(user_id)})).first()
        if row is None:
            return None
        return ViewedPerson(
            person_id=row.id, display_name=row.display_name, notice_at=row.tracing_notice_at
        )

    async def voice(self, window: UsageWindow) -> VoiceUsage:
        bounds = {"first_month": _month(window.start.date()), "until": window.end.date()}
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sql.VOICE_BY_PERSON, bounds)).all()
            anonymous = await conn.scalar(sql.VOICE_ANONYMOUS, bounds)
        return VoiceUsage(
            by_user={row.user_id: int(row.seconds) for row in rows if row.user_id},
            anonymous=int(anonymous or 0),
        )


def _month(day: date) -> date:
    return day.replace(day=1)
