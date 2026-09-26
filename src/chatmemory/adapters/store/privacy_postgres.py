"""Implements `PrivacyStore`. The statements live next door."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from chatmemory.adapters.store import privacy_sql as sql
from chatmemory.adapters.store import retention_sql
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.facts import FactKind
from chatmemory.ports.privacy import (
    RECENT_QUESTIONS,
    ArchivedChannel,
    HeldAlert,
    HeldFact,
    HeldSuggestion,
    HeldTask,
    HeldToken,
    Inventory,
    MediaCounts,
    MemoryCounts,
    NotificationSetting,
    PrivacyStore,
)

#: A kind a later release dropped is skipped rather than failing the whole view.
_KNOWN_KINDS = frozenset(k.value for k in FactKind)


class PostgresPrivacyStore:
    """Implements `PrivacyStore`, in one read-only transaction."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def inventory(
        self, person: PersonRef, readable_channel_ids: Sequence[int], month: date
    ) -> Inventory:
        async with self._engine.connect() as conn:
            found = await conn.scalar(
                retention_sql.PERSON_BY_PLATFORM_ID,
                {"platform": person.platform, "platform_user_id": person.platform_user_id},
            )
            if found is None:
                return Inventory()
            reader = _Reader(conn, int(found))
            channel_ids = sorted(set(readable_channel_ids))
            return Inventory(
                known=True,
                platforms=await reader.platforms(),
                archiving=not await reader.scalar(sql.IS_OPTED_OUT),
                facts=await reader.facts(),
                memory=await reader.memory(),
                recent_questions=await reader.recent_questions(),
                tasks=await reader.tasks(),
                alerts=await reader.alerts(),
                notifications=await reader.notifications(),
                voice_seconds_this_month=int(await reader.scalar(sql.VOICE_SECONDS, month=month)),
                media=await reader.media(channel_ids),
                suggestions=await reader.suggestions(),
                tokens=await reader.tokens(),
                archived=await reader.archived(person.platform, channel_ids),
                traces=int(await reader.scalar(sql.TRACES)),
            )


class _Reader:
    """One person's rows, one store at a time, over one connection."""

    def __init__(self, conn: AsyncConnection, person_id: int) -> None:
        self._conn = conn
        self._person_id = person_id

    async def _rows(self, statement: TextClause, **params: object) -> Sequence[RowMapping]:
        result = await self._conn.execute(
            statement,
            {"person_id": self._person_id, **params},
        )
        return list(result.mappings())

    async def scalar(self, statement: TextClause, **params: object) -> Any:
        return await self._conn.scalar(
            statement,
            {"person_id": self._person_id, **params},
        )

    async def platforms(self) -> tuple[str, ...]:
        return tuple(str(r["platform"]) for r in await self._rows(sql.PLATFORMS))

    async def facts(self) -> tuple[HeldFact, ...]:
        rows = await self._rows(sql.FACTS)
        return tuple(
            HeldFact(FactKind(r["kind"]), str(r["value"]))
            for r in rows
            if r["kind"] in _KNOWN_KINDS
        )

    async def memory(self) -> MemoryCounts:
        counts = {
            bool(r["direct"]): (int(r["turns"]), int(r["summaries"]))
            for r in await self._rows(sql.MEMORY_COUNTS)
        }
        direct, channel = counts.get(True, (0, 0)), counts.get(False, (0, 0))
        return MemoryCounts(direct[0], direct[1], channel[0], channel[1])

    async def recent_questions(self) -> tuple[str, ...]:
        rows = await self._rows(sql.RECENT_QUESTIONS, limit=RECENT_QUESTIONS)
        return tuple(str(r["question"]) for r in rows)

    async def tasks(self) -> tuple[HeldTask, ...]:
        return tuple(
            HeldTask(
                id=int(r["id"]),
                question=str(r["question"]),
                interval_hours=int(r["interval_hours"]),
                last_outcome=r["last_outcome"],
                disabled=bool(r["disabled"]),
            )
            for r in await self._rows(sql.TASKS)
        )

    async def alerts(self) -> tuple[HeldAlert, ...]:
        return tuple(
            HeldAlert(
                id=int(r["id"]),
                kind=str(r["kind"]),
                chain=r["chain"],
                address=r["address"],
                asset=r["asset"],
            )
            for r in await self._rows(sql.ALERTS)
        )

    async def notifications(self) -> NotificationSetting:
        [row] = await self._rows(sql.NOTIFICATIONS)
        enabled = row["enabled"]
        return NotificationSetting(
            enabled=None if enabled is None else bool(enabled),
            queued=int(row["queued"]),
        )

    async def media(self, channel_ids: Sequence[int]) -> MediaCounts:
        if not channel_ids:
            return MediaCounts()
        rows = await self._rows(sql.MEDIA, channel_ids=list(channel_ids))
        return MediaCounts(
            by_kind=tuple((str(r["kind"]), int(r["rows"])) for r in rows),
            with_text=sum(int(r["with_text"]) for r in rows),
        )

    async def suggestions(self) -> tuple[HeldSuggestion, ...]:
        return tuple(
            HeldSuggestion(int(r["id"]), str(r["text"]), str(r["status"]))
            for r in await self._rows(sql.SUGGESTIONS)
        )

    async def tokens(self) -> tuple[HeldToken, ...]:
        return tuple(
            HeldToken(r["label"], r["issued_at"])
            for r in await self._rows(sql.TOKENS)
        )

    async def archived(
        self, platform: str, channel_ids: Sequence[int]
    ) -> tuple[ArchivedChannel, ...]:
        if not channel_ids:
            return ()
        rows = await self._rows(sql.ARCHIVED_BY_CHANNEL, channel_ids=list(channel_ids))
        return tuple(
            ArchivedChannel(ChannelRef(platform, int(r["channel_id"])), int(r["messages"]))
            for r in rows
        )


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresPrivacyStore) -> PrivacyStore:
        return store
