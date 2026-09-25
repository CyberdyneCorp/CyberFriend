"""Who gets traced, and who does not.

Kept apart from `ReasoningAnswerService` so the answering path does not acquire
a dependency on the opt-out registry to satisfy a rule about exporting. The
service calls a tracer; whether this particular run is one to export is this
layer's decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import structlog

from chatmemory.app.optout import OptOutRegistry
from chatmemory.app.reasoning.contract import RunTrace, RunTracer
from chatmemory.ports.tracing import TraceDeleter, TraceFinder, TraceIndex

log = structlog.get_logger()


class OptOutAwareTracer:
    """Exports every run except one whose asker opted out of indexing.

    An opt-out removes a person's messages from the archive. Exporting their
    questions and answers to a second store would put back, somewhere with no
    viewer scoping at all, exactly what they asked to have taken out.

    A registry that cannot answer is treated as an opt-out. Failing the other
    way would export on exactly the errors nobody notices.
    """

    def __init__(self, inner: RunTracer, registry: OptOutRegistry) -> None:
        self._inner = inner
        self._registry = registry

    async def trace(self, run: RunTrace) -> None:
        person = run.question.asker.person
        try:
            if await self._registry.is_opted_out(person):
                return
        except Exception as exc:  # noqa: BLE001 - never fail the answer path
            log.warning("tracing.optout_check_failed", error=str(exc))
            return
        await self._inner.trace(run)


class TraceWithdrawal:
    """Implements `TraceSink`: deletes the traces quoting a deleted message.

    Two steps rather than one, and the order is the point. The index is marked
    first and the destination called second, so a destination that is down
    leaves durable work to retry rather than a log line nobody reads. Only a
    confirmed deletion clears the mark.
    """

    def __init__(
        self,
        index: TraceIndex,
        deleter: TraceDeleter,
        finder: TraceFinder | None = None,
    ) -> None:
        self._index = index
        self._deleter = deleter
        self._finder = finder

    async def withdraw_message(self, message_id: int) -> None:
        trace_ids = await self._index.request_deletion_for_message(message_id)
        if not trace_ids:
            return
        await self._flush(trace_ids)

    async def retry_pending(self, limit: int = 100) -> int:
        """Re-attempt deletions the destination has not confirmed.

        The other half of failing safe. Without this, a trace store that was
        down for the minute somebody deleted a message keeps that message
        forever, and nothing ever looks again.
        """
        pending = await self._index.pending_deletions(limit)
        if not pending:
            return 0
        return len(pending) if await self._flush(pending) else 0

    async def search_askers(self, limit: int = 10) -> int:
        """Find opted-out askers' traces the index never recorded; mark them.

        The backstop for traces exported before the index recorded who asked
        them. The deletion itself is left to `retry_pending`, so a search and
        a deletion fail and retry independently. A search the destination
        could not answer stays open for the next pass.
        """
        if self._finder is None:
            return 0
        found = 0
        for platform_user_id in await self._index.open_asker_searches(limit):
            started = datetime.now(UTC)
            trace_ids = await self._finder.find_traces_by_user(platform_user_id)
            if trace_ids is None:
                log.warning("tracing.asker_search_deferred")
                continue
            await self._index.record_found_traces(platform_user_id, trace_ids, started)
            found += len(trace_ids)
        return found

    async def _flush(self, trace_ids: Sequence[str]) -> bool:
        if not await self._deleter.delete_traces(trace_ids):
            log.warning("tracing.withdrawal_deferred", count=len(trace_ids))
            return False
        await self._index.confirm_deleted(trace_ids)
        log.info("tracing.withdrawn", count=len(trace_ids))
        return True
