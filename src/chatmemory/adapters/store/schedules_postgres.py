"""Implements `ScheduleStore`. The statements live next door in `schedules_sql`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import structlog
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import schedules_sql
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.schedules import (
    DueTask,
    ScheduledTask,
    ScheduleStore,
    TaskOutcome,
)

log = structlog.get_logger()

PLATFORM = "discord"
DEFAULT_CAP = 5


def _task(row: RowMapping) -> ScheduledTask:
    outcome = row["last_outcome"]
    return ScheduledTask(
        id=row["id"],
        question=row["question"],
        interval_hours=row["interval_hours"],
        next_run_at=row["next_run_at"],
        created_at=row["created_at"],
        last_run_at=row["last_run_at"],
        last_outcome=TaskOutcome(outcome) if outcome else None,
        disabled_at=row["disabled_at"],
        disabled_reason=row["disabled_reason"] or "",
    )


class PostgresScheduleStore:
    """Implements `ScheduleStore`."""

    def __init__(
        self, engine: AsyncEngine, cap: int = DEFAULT_CAP, platform: str = PLATFORM
    ) -> None:
        self._engine = engine
        # The per-person cap, enforced inside the insert rather than read and
        # then checked: two commands arriving together would each read the
        # same count and both write.
        self._cap = cap
        self._platform = platform

    async def _person_id(self, conn: object, person: PersonRef) -> int | None:
        rows = await conn.execute(  # type: ignore[attr-defined]
            schedules_sql.RESOLVE_PERSON_ID,
            {"platform": self._platform, "platform_user_id": person.platform_user_id},
        )
        found = rows.first()
        return None if found is None else int(found[0])

    async def create(
        self, person: PersonRef, question: str, interval_hours: int, first_run_at: datetime
    ) -> ScheduledTask | None:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                # Nobody the corpus has ever seen. Refusing beats creating a
                # person row here: this is not a path that should be able to
                # invent identities.
                return None
            rows = await conn.execute(
                schedules_sql.CREATE_WITHIN_CAP,
                {
                    "person_id": person_id,
                    "question": question,
                    "interval_hours": interval_hours,
                    "first_run_at": first_run_at,
                    "cap": self._cap,
                },
            )
            row = rows.mappings().first()
            return None if row is None else _task(row)

    async def for_person(self, person: PersonRef) -> Sequence[ScheduledTask]:
        async with self._engine.connect() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return []
            rows = await conn.execute(schedules_sql.FOR_PERSON, {"person_id": person_id})
            return [_task(r) for r in rows.mappings()]

    async def delete(self, person: PersonRef, task_id: int) -> bool:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return False
            deleted = await conn.execute(
                schedules_sql.DELETE_OWN, {"task_id": task_id, "person_id": person_id}
            )
            return bool(deleted.rowcount)

    async def claim_due(self, now: datetime, limit: int) -> Sequence[DueTask]:
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                schedules_sql.CLAIM_DUE,
                {"now": now, "limit": limit, "platform": self._platform},
            )
            claimed = []
            for row in rows.mappings():
                platform_user_id = row["platform_user_id"]
                if platform_user_id is None:
                    # A person with no platform identity cannot be messaged,
                    # so there is nowhere to send the answer. Skipped rather
                    # than run: the run would cost a model call and then be
                    # discarded.
                    log.warning("schedules.owner_unresolvable", task_id=row["id"])
                    continue
                claimed.append(
                    DueTask(
                        id=row["id"],
                        person=PersonRef(
                            platform=self._platform, platform_user_id=int(platform_user_id)
                        ),
                        question=row["question"],
                    )
                )
            return claimed

    async def record_run(self, task_id: int, outcome: TaskOutcome, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                schedules_sql.RECORD_RUN,
                {"task_id": task_id, "outcome": str(outcome), "now": now},
            )

    async def disable(self, person: PersonRef, reason: str, now: datetime) -> int:
        async with self._engine.begin() as conn:
            person_id = await self._person_id(conn, person)
            if person_id is None:
                return 0
            stopped = await conn.execute(
                schedules_sql.DISABLE_FOR_PERSON,
                {"person_id": person_id, "reason": reason, "now": now},
            )
            return int(stopped.rowcount or 0)


def _conforms(store: PostgresScheduleStore) -> ScheduleStore:
    return store
