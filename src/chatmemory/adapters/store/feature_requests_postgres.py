"""Implements `FeatureRequestStore`. The statements live next door."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import feature_requests_sql as sql
from chatmemory.adapters.store import notify_sql, retention_sql
from chatmemory.adapters.store.memory_postgres import _person_id
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.feature_requests import (
    FeatureRequest,
    FeatureRequestStore,
    FeatureRequestTriage,
    NewSuggestion,
    RequestStatus,
    SourceKind,
    StatusNews,
    StatusNewsStore,
    StoreResult,
    StoreVerdict,
    TriageChange,
    TriageEntry,
    TriagePage,
    TriageRefusal,
    TriageResult,
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
            # One submission per person at a time, so the limit's count holds.
            await conn.execute(sql.LOCK_PERSON, {"person_id": person_id})
            # Before the name: nothing of an opted-out person is written.
            if await conn.scalar(sql.IS_OPTED_OUT, {"person_id": person_id}):
                return StoreResult(StoreVerdict.OPTED_OUT)
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


def _triage_entry(row: RowMapping) -> TriageEntry:
    return TriageEntry(
        id=int(row["id"]),
        text=str(row["text"]),
        language=row["language"],
        status=RequestStatus(str(row["status"])),
        admin_note=row["admin_note"],
        duplicate_of=None if row["duplicate_of"] is None else int(row["duplicate_of"]),
        source_kind=SourceKind(str(row["source_kind"])),
        person_name=str(row["person_name"]),
        same_text_elsewhere=int(row["same_text_elsewhere"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


def _applied(before: TriageEntry, change: TriageChange) -> dict[str, object]:
    """The row's triage fields once `change` is applied to `before`."""
    return {
        "status": (change.status or before.status).value,
        "admin_note": change.admin_note if change.set_note else before.admin_note,
        "duplicate_of": change.duplicate_of if change.set_duplicate else before.duplicate_of,
    }


class PostgresFeatureRequestTriage:
    """Implements `FeatureRequestTriage`, for the admin console."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def triage_page(
        self, status: RequestStatus | None, *, offset: int, limit: int
    ) -> TriagePage:
        wanted = None if status is None else status.value
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sql.TRIAGE_PAGE, {"status": wanted, "offset": offset, "limit": limit}
            )
            entries = [_triage_entry(r) for r in rows.mappings()]
            total = await conn.scalar(sql.TRIAGE_COUNT, {"status": wanted})
        return TriagePage(entries=entries, total=int(total or 0))

    async def triage(
        self, request_id: int, change: TriageChange, *, actor: str, now: datetime
    ) -> TriageResult:
        async with self._engine.begin() as conn:
            before = await _locked_row(conn, request_id)
            if before is None:
                return TriageResult(refusal=TriageRefusal.NOT_FOUND)
            target = change.duplicate_of if change.set_duplicate else None
            if target is not None and not await conn.scalar(
                sql.TRIAGE_EXISTS, {"request_id": target}
            ):
                return TriageResult(before=before, refusal=TriageRefusal.UNKNOWN_DUPLICATE)
            updated = await conn.execute(
                sql.TRIAGE_UPDATE,
                {
                    **_applied(before, change),
                    "request_id": request_id,
                    "now": now,
                    "updated_by": actor,
                },
            )
            # The opt-out trigger drops an update to an opted-out person's row;
            # their rows are purged with the opt-out, so this is a race at most.
            if not updated.rowcount:
                return TriageResult(refusal=TriageRefusal.NOT_FOUND)
            after = await _locked_row(conn, request_id)
        return TriageResult(before=before, after=after)


async def _locked_row(conn: AsyncConnection, request_id: int) -> TriageEntry | None:
    row = (await conn.execute(sql.TRIAGE_ROW, {"request_id": request_id})).mappings().first()
    return None if row is None else _triage_entry(row)


class PostgresStatusNewsStore:
    """Implements `StatusNewsStore`, for the bot's status sweep."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def claim_status_news(self, limit: int) -> Sequence[StatusNews]:
        async with self._engine.begin() as conn:
            rows = await conn.execute(sql.CLAIM_STATUS_NEWS, {"limit": limit})
            return [
                StatusNews(
                    request_id=int(r["id"]),
                    person=PersonRef(str(r["platform"]), int(r["platform_user_id"])),
                    status=RequestStatus(str(r["status"])),
                    previous=str(r["previous"]),
                    language=r["language"],
                    text=str(r["text"]),
                )
                for r in rows.mappings()
            ]

    async def release(self, news: StatusNews) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sql.RELEASE_STATUS_NEWS,
                {
                    "request_id": news.request_id,
                    "previous": news.previous,
                    "claimed": news.status.value,
                },
            )

    async def record_undeliverable(self, person: PersonRef, now: datetime) -> None:
        # The notification queue's own record, so `/notifications on` reopens
        # both, and their queued notifications are settled as it would do.
        async with self._engine.begin() as conn:
            person_id = await _known_person(conn, person)
            if person_id is None:
                return
            params = {"person_id": person_id, "now": now}
            await conn.execute(notify_sql.RECORD_UNDELIVERABLE, params)
            await conn.execute(notify_sql.SETTLE_UNDELIVERABLE, params)


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresFeatureRequestStore) -> FeatureRequestStore:
        return store

    def _triages(store: PostgresFeatureRequestTriage) -> FeatureRequestTriage:
        return store

    def _news(store: PostgresStatusNewsStore) -> StatusNewsStore:
        return store
