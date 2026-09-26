"""The durable half of "deleting a message deletes the traces quoting it".

The bot writes here when it exports; `ingest` reads here when a message is
deleted. Neither process can see the other, so this table is the whole of the
connection between them.

Every method is written so that a failure costs a trace rather than an answer
or a deletion. `record_export` swallows: losing the mapping leaves a trace that
outlives its message, which is bad, but raising would cost the requester their
reply for a bookkeeping error. `request_deletion_for_message` is called from
the tombstone path and must never be the reason a message survives.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.domain.identity import PersonRef

log = structlog.get_logger()

#: A trace whose asker opted out while the run was answering is born pending:
#: the opt-out has already marked that person's traces and would miss this one.
RECORD_TRACE = text("""
INSERT INTO trace_export (trace_id, created_at, asker_platform_user_id, deletion_requested_at)
VALUES (
    :trace_id, :now, CAST(:asker AS bigint),
    CASE WHEN EXISTS (
        SELECT 1 FROM person_platform_id p
        JOIN person_opt_out o ON o.person_id = p.person_id
        WHERE p.platform = :platform AND p.platform_user_id = CAST(:asker AS bigint)
    ) THEN CAST(:now AS timestamptz) END
)
ON CONFLICT (trace_id) DO NOTHING
""")

RECORD_MESSAGE = text("""
INSERT INTO trace_export_message (trace_id, platform_message_id)
VALUES (:trace_id, :message_id)
ON CONFLICT DO NOTHING
""")

#: Mark every trace quoting this message, and hand back what to delete.
#:
#: The UPDATE returns the ids rather than a separate SELECT doing so: two
#: statements would let a concurrent sweep claim the same rows, and a trace
#: deleted twice is a 404 the caller has to learn to ignore.
#:
#: `deletion_requested_at` is only ever set, never cleared. A trace asked to be
#: deleted stays asked-for until the destination confirms, which is what makes
#: a failed delete a retry rather than a loss.
REQUEST_DELETION = text("""
UPDATE trace_export SET deletion_requested_at = COALESCE(deletion_requested_at, :now)
WHERE deleted_at IS NULL
  AND trace_id IN (
      SELECT trace_id FROM trace_export_message WHERE platform_message_id = :message_id
  )
RETURNING trace_id
""")

REQUEST_DELETION_FOR_ASKER = text("""
UPDATE trace_export SET deletion_requested_at = COALESCE(deletion_requested_at, :now)
WHERE deleted_at IS NULL AND asker_platform_user_id = ANY(:askers)
RETURNING trace_id
""")

#: Every platform id of the person, found by any one of them. An unknown
#: person has only the id they were named by.
PERSON_PLATFORM_IDS = text("""
SELECT p.person_id, p.platform_user_id FROM person_platform_id p
WHERE p.person_id = (
    SELECT person_id FROM person_platform_id
    WHERE platform = :platform AND platform_user_id = :platform_user_id
)
""")

#: Traces quoting a message the person wrote. Run before their messages are
#: purged, because the message row is what says who wrote it.
REQUEST_DELETION_FOR_AUTHOR = text("""
UPDATE trace_export SET deletion_requested_at = COALESCE(deletion_requested_at, :now)
WHERE deleted_at IS NULL
  AND trace_id IN (
      SELECT tm.trace_id FROM trace_export_message tm
      JOIN message m ON m.id = tm.platform_message_id
      WHERE m.author_person_id = :person_id
  )
RETURNING trace_id
""")

#: Reopens a finished search: a person who opted in and out again may have
#: been traced in between.
QUEUE_ASKER_SEARCH = text("""
INSERT INTO trace_asker_search (platform_user_id, requested_at)
VALUES (:platform_user_id, :now)
ON CONFLICT (platform_user_id)
DO UPDATE SET requested_at = EXCLUDED.requested_at, completed_at = NULL
""")

OPEN_ASKER_SEARCHES = text("""
SELECT platform_user_id FROM trace_asker_search
WHERE completed_at IS NULL
ORDER BY requested_at, platform_user_id
LIMIT :limit
""")

#: A trace found at the destination but never indexed here. One already
#: confirmed deleted is left alone: Langfuse deletes asynchronously and may
#: still list it.
RECORD_FOUND_TRACE = text("""
INSERT INTO trace_export (trace_id, created_at, asker_platform_user_id, deletion_requested_at)
VALUES (:trace_id, :now, :asker, :now)
ON CONFLICT (trace_id) DO UPDATE
SET deletion_requested_at =
        COALESCE(trace_export.deletion_requested_at, EXCLUDED.deletion_requested_at),
    asker_platform_user_id =
        COALESCE(trace_export.asker_platform_user_id, EXCLUDED.asker_platform_user_id)
WHERE trace_export.deleted_at IS NULL
""")

#: Only a search requested before it started is closed: an opt-out that
#: reopened the row meanwhile gets a fresh search. Closed by deleting the row:
#: a finished search keyed on a platform id is a record that the person was
#: withdrawn, which an erasure must not leave behind. Queueing again inserts.
COMPLETE_ASKER_SEARCH = text("""
DELETE FROM trace_asker_search
WHERE platform_user_id = :platform_user_id AND completed_at IS NULL
  AND requested_at <= :started
""")

#: Retention: every live trace exported before the cutoff. Rows already marked
#: are left out so that what is returned is what this sweep newly expired.
REQUEST_DELETION_BEFORE = text("""
UPDATE trace_export SET deletion_requested_at = :now
WHERE deleted_at IS NULL AND deletion_requested_at IS NULL AND created_at < :cutoff
RETURNING trace_id
""")

#: A trace the destination still holds past retention. Unlike
#: `RECORD_FOUND_TRACE` the asker is unknown, and one already confirmed deleted
#: is left alone for the same reason: Langfuse may still list it.
RECORD_EXPIRED_TRACE = text("""
INSERT INTO trace_export (trace_id, created_at, deletion_requested_at)
VALUES (:trace_id, :now, :now)
ON CONFLICT (trace_id) DO UPDATE
SET deletion_requested_at =
        COALESCE(trace_export.deletion_requested_at, EXCLUDED.deletion_requested_at)
WHERE trace_export.deleted_at IS NULL
""")

#: A confirmed deletion keeps only the trace id and when it went, so that a
#: listing Langfuse has not caught up with does not queue it again. Who asked
#: and which messages it quoted go: they were kept only to find the trace, and
#: left behind they are a per-person record of when someone asked questions.
CONFIRM_DELETED = text("""
WITH confirmed AS (
    UPDATE trace_export SET deleted_at = :now, asker_platform_user_id = NULL
    WHERE trace_id = ANY(:trace_ids) AND deleted_at IS NULL
    RETURNING trace_id
)
DELETE FROM trace_export_message
WHERE trace_id IN (SELECT trace_id FROM confirmed)
""")

PENDING_DELETIONS = text("""
SELECT trace_id FROM trace_export
WHERE deletion_requested_at IS NOT NULL AND deleted_at IS NULL
-- Tie-broken on the id: two deletions requested in the same transaction share
-- a timestamp, and a sweep whose page boundary falls inside that tie would
-- skip rows it never returned.
ORDER BY deletion_requested_at, trace_id
LIMIT :limit
""")


class PostgresTraceIndex:
    """Implements `TraceIndex`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record_export(
        self,
        trace_id: str,
        message_ids: Sequence[int],
        asker: PersonRef | None = None,
    ) -> None:
        now = datetime.now(UTC)
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    RECORD_TRACE,
                    {
                        "trace_id": trace_id,
                        "now": now,
                        "asker": asker.platform_user_id if asker else None,
                        "platform": asker.platform if asker else None,
                    },
                )
                if message_ids:
                    await conn.execute(
                        RECORD_MESSAGE,
                        [
                            {"trace_id": trace_id, "message_id": int(m)}
                            for m in sorted(set(message_ids))
                        ],
                    )
        except Exception as exc:  # noqa: BLE001 - never fail the answer path
            log.warning("tracing.index_write_failed", trace_id=trace_id, error=str(exc))

    async def request_deletion_for_message(self, message_id: int) -> Sequence[str]:
        try:
            async with self._engine.begin() as conn:
                rows = await conn.execute(
                    REQUEST_DELETION, {"message_id": int(message_id), "now": datetime.now(UTC)}
                )
                return [str(r[0]) for r in rows]
        except Exception as exc:  # noqa: BLE001 - never block a tombstone
            log.warning("tracing.deletion_request_failed", message_id=message_id, error=str(exc))
            return []

    async def request_deletion_for_asker(
        self, platform_user_ids: Sequence[int]
    ) -> Sequence[str]:
        async with self._engine.begin() as conn:
            return await _mark_asked(conn, platform_user_ids, datetime.now(UTC))

    async def request_deletion_for_person(self, person: PersonRef) -> int:
        """Implements `PersonTraces`: asked and quoting traces, then the search.

        Raises rather than swallows, unlike the tombstone path: the caller is
        an opt-out, and one that reports success with the traces left behind
        is the failure this exists to prevent.
        """
        now = datetime.now(UTC)
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    PERSON_PLATFORM_IDS,
                    {"platform": person.platform, "platform_user_id": person.platform_user_id},
                )
            ).all()
            person_id = int(rows[0][0]) if rows else None
            askers = sorted({int(r[1]) for r in rows} | {person.platform_user_id})
            marked = set(await _mark_asked(conn, askers, now))
            if person_id is not None:
                quoting = await conn.execute(
                    REQUEST_DELETION_FOR_AUTHOR, {"person_id": person_id, "now": now}
                )
                marked.update(str(r[0]) for r in quoting)
            await conn.execute(
                QUEUE_ASKER_SEARCH, [{"platform_user_id": a, "now": now} for a in askers]
            )
            return len(marked)

    async def open_asker_searches(self, limit: int) -> Sequence[int]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(OPEN_ASKER_SEARCHES, {"limit": limit})
            return [int(r[0]) for r in rows]

    async def record_found_traces(
        self, platform_user_id: int, trace_ids: Sequence[str], started: datetime
    ) -> None:
        now = datetime.now(UTC)
        async with self._engine.begin() as conn:
            if trace_ids:
                await conn.execute(
                    RECORD_FOUND_TRACE,
                    [
                        {"trace_id": t, "now": now, "asker": platform_user_id}
                        for t in sorted(set(trace_ids))
                    ],
                )
            await conn.execute(
                COMPLETE_ASKER_SEARCH,
                {"platform_user_id": platform_user_id, "now": now, "started": started},
            )

    async def request_deletion_before(self, cutoff: datetime) -> Sequence[str]:
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                REQUEST_DELETION_BEFORE, {"cutoff": cutoff, "now": datetime.now(UTC)}
            )
            return [str(r[0]) for r in rows]

    async def record_expired_traces(self, trace_ids: Sequence[str]) -> None:
        if not trace_ids:
            return
        now = datetime.now(UTC)
        async with self._engine.begin() as conn:
            await conn.execute(
                RECORD_EXPIRED_TRACE,
                [{"trace_id": t, "now": now} for t in sorted(set(trace_ids))],
            )

    async def confirm_deleted(self, trace_ids: Sequence[str]) -> None:
        if not trace_ids:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                CONFIRM_DELETED, {"trace_ids": list(trace_ids), "now": datetime.now(UTC)}
            )

    async def pending_deletions(self, limit: int) -> Sequence[str]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(PENDING_DELETIONS, {"limit": limit})
            return [str(r[0]) for r in rows]


async def _mark_asked(
    conn: AsyncConnection, platform_user_ids: Sequence[int], now: datetime
) -> list[str]:
    if not platform_user_ids:
        return []
    rows = await conn.execute(
        REQUEST_DELETION_FOR_ASKER,
        {"askers": [int(a) for a in platform_user_ids], "now": now},
    )
    return [str(r[0]) for r in rows]
