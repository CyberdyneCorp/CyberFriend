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
from sqlalchemy.ext.asyncio import AsyncEngine

log = structlog.get_logger()

RECORD_TRACE = text("""
INSERT INTO trace_export (trace_id, created_at) VALUES (:trace_id, :now)
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

CONFIRM_DELETED = text("""
UPDATE trace_export SET deleted_at = :now
WHERE trace_id = ANY(:trace_ids) AND deleted_at IS NULL
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

    async def record_export(self, trace_id: str, message_ids: Sequence[int]) -> None:
        now = datetime.now(UTC)
        try:
            async with self._engine.begin() as conn:
                await conn.execute(RECORD_TRACE, {"trace_id": trace_id, "now": now})
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
