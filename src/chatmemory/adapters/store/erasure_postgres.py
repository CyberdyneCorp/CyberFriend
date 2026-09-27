"""Implements `ErasureStore`. The statements live next door."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import erasure_sql as sql
from chatmemory.adapters.store import media_sql
from chatmemory.adapters.store.retention_sql import _person_id
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.privacy import (
    ERASED_NAME,
    ErasureCounts,
    ErasureMode,
    ErasureRequest,
    ErasureStep,
    ErasureStore,
)


class PostgresErasureStore:
    """Implements `ErasureStore`, one transaction per step."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def open(
        self, person: PersonRef, mode: ErasureMode, counts: ErasureCounts
    ) -> ErasureRequest:
        async with self._engine.begin() as conn:
            person_id = await _person_id(conn, person)
            row = (
                await conn.execute(
                    sql.OPEN_REQUEST,
                    {
                        "person_id": person_id,
                        "mode": mode.value,
                        "counts": json.dumps(counts.as_json()),
                    },
                )
            ).mappings().one()
        return _request(row, person)

    async def stop_reimport(self, request: ErasureRequest) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sql.STOP_REIMPORT,
                {"person_id": request.person_id, "requested_at": request.requested_at},
            )

    async def record_traces(self, request: ErasureRequest, traces: int) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sql.RECORD_TRACES, {"id": request.id, "traces": traces})

    async def purge_derived(self, request: ErasureRequest) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sql.PURGE_DERIVED, {"person_id": request.person_id})

    async def fold_voice(self, request: ErasureRequest) -> int:
        async with self._engine.begin() as conn:
            await conn.execute(media_sql.LOCK_LEDGER)
            moved = await conn.scalar(sql.FOLD_VOICE, {"person_id": request.person_id})
        return int(moved or 0)

    async def tombstone(self, request: ErasureRequest) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sql.TOMBSTONE_PERSON,
                {"person_id": request.person_id, "placeholder": ERASED_NAME},
            )
            await conn.execute(sql.RESET_PREFERENCES, {"person_id": request.person_id})

    async def advance(self, request: ErasureRequest, step: ErasureStep) -> ErasureRequest:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(sql.ADVANCE, {"id": request.id, "step": int(step)})
            ).mappings().one()
        return _request(row, request.person)

    async def open_requests(self, idle_since: datetime, limit: int) -> Sequence[ErasureRequest]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(sql.OPEN_REQUESTS, {"idle_since": idle_since, "limit": limit})
            ).mappings().all()
        return [
            _request(row, PersonRef(str(row["platform"]), int(row["platform_user_id"])))
            for row in rows
        ]


def _request(row: RowMapping, person: PersonRef) -> ErasureRequest:
    return ErasureRequest(
        id=int(row["id"]),
        person_id=int(row["person_id"]),
        person=person,
        mode=ErasureMode(row["mode"]),
        step=ErasureStep(int(row["step"])),
        requested_at=row["requested_at"],
        counts=ErasureCounts.from_json(row["counts"] or {}),
    )


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresErasureStore) -> ErasureStore:
        return store
