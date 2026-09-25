"""Implements `FeatureRequestStore`. The statements live next door."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import feature_requests_sql as sql
from chatmemory.adapters.store import retention_sql
from chatmemory.adapters.store.memory_postgres import _person_id
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.feature_requests import (
    FeatureRequest,
    FeatureRequestStore,
    NewSuggestion,
    RequestStatus,
    StoreResult,
    StoreVerdict,
)


def _request(row: RowMapping) -> FeatureRequest:
    return FeatureRequest(
        id=int(row["id"]),
        text=str(row["text"]),
        status=RequestStatus(str(row["status"])),
        created_at=row["created_at"],
        notify_on_change=bool(row["notify_on_change"]),
    )


class PostgresFeatureRequestStore:
    """Implements `FeatureRequestStore`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def submit(
        self, suggestion: NewSuggestion, *, now: datetime, daily_limit: int
    ) -> StoreResult:
        async with self._engine.begin() as conn:
            # Created on miss, as memory and facts do: somebody may suggest
            # something before ingest has ever seen them post.
            person_id = await _person_id(conn, suggestion.person)
            await self._name(conn, person_id, suggestion)
            existing = await self._existing(conn, person_id, suggestion.normalized_hash)
            if existing is not None:
                return StoreResult(StoreVerdict.DUPLICATE, existing)
            inserted = await conn.execute(
                sql.INSERT_WITHIN_LIMIT, _insert_params(person_id, suggestion, now, daily_limit)
            )
            new_id = inserted.scalar()
            if new_id is not None:
                return StoreResult(StoreVerdict.STORED, int(new_id))
            return await self._why_not_stored(conn, person_id, suggestion)

    async def _why_not_stored(
        self, conn: AsyncConnection, person_id: int, suggestion: NewSuggestion
    ) -> StoreResult:
        # The insert returned nothing: a concurrent twin won the unique key,
        # the opt-out trigger dropped the row, or the limit was reached.
        existing = await self._existing(conn, person_id, suggestion.normalized_hash)
        if existing is not None:
            return StoreResult(StoreVerdict.DUPLICATE, existing)
        if await conn.scalar(sql.IS_OPTED_OUT, {"person_id": person_id}):
            return StoreResult(StoreVerdict.OPTED_OUT)
        return StoreResult(StoreVerdict.LIMITED)

    @staticmethod
    async def _existing(conn: AsyncConnection, person_id: int, digest: bytes) -> int | None:
        found = await conn.scalar(
            sql.EXISTING_BY_HASH, {"person_id": person_id, "normalized_hash": digest}
        )
        return None if found is None else int(found)

    @staticmethod
    async def _name(conn: AsyncConnection, person_id: int, suggestion: NewSuggestion) -> None:
        if not suggestion.display_name:
            return
        await conn.execute(
            sql.NAME_PLACEHOLDER_PERSON,
            {
                "person_id": person_id,
                "display_name": suggestion.display_name,
                "placeholder": str(suggestion.person.platform_user_id),
            },
        )

    async def for_person(self, person: PersonRef, limit: int) -> Sequence[FeatureRequest]:
        async with self._engine.connect() as conn:
            person_id = await _known_person(conn, person)
            if person_id is None:
                return []
            rows = await conn.execute(sql.FOR_PERSON, {"person_id": person_id, "limit": limit})
            return [_request(r) for r in rows.mappings()]

    async def set_notify(self, person: PersonRef, request_id: int, notify: bool) -> bool:
        async with self._engine.begin() as conn:
            person_id = await _known_person(conn, person)
            if person_id is None:
                return False
            changed = await conn.execute(
                sql.SET_NOTIFY,
                {"person_id": person_id, "request_id": request_id, "notify": notify},
            )
            return bool(changed.rowcount)


async def _known_person(conn: AsyncConnection, person: PersonRef) -> int | None:
    """The person's id, without creating one: reading must not invent people."""
    found = await conn.scalar(
        retention_sql.PERSON_BY_PLATFORM_ID,
        {"platform": person.platform, "platform_user_id": person.platform_user_id},
    )
    return None if found is None else int(found)


def _insert_params(
    person_id: int, suggestion: NewSuggestion, now: datetime, daily_limit: int
) -> dict[str, object]:
    source = suggestion.source
    return {
        "person_id": person_id,
        "text": suggestion.text,
        "normalized_hash": suggestion.normalized_hash,
        "language": suggestion.language,
        "source_kind": source.kind.value,
        "platform": source.platform,
        "guild_id": source.guild_id,
        "channel_id": source.channel_id,
        "now": now,
        "daily_limit": daily_limit,
    }


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresFeatureRequestStore) -> FeatureRequestStore:
        return store
