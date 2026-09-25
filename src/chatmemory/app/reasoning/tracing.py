"""Who gets traced, and who does not.

Kept apart from `ReasoningAnswerService` so the answering path does not acquire
a dependency on the opt-out registry to satisfy a rule about exporting. The
service calls a tracer; whether this particular run is one to export is this
layer's decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from chatmemory.app.language import Language, detect
from chatmemory.app.optout import OptOutRegistry
from chatmemory.app.reasoning.contract import (
    RunAnswerService,
    RunOutcome,
    RunTrace,
    RunTracer,
)
from chatmemory.ports.answers import Answer, Question
from chatmemory.ports.tracing import (
    ExpiredTraceFinder,
    TraceDeleter,
    TraceFinder,
    TraceIndex,
)

log = structlog.get_logger()

LANGUAGE_CODES = {Language.ENGLISH: "en", Language.PORTUGUESE: "pt"}
"""The short code a trace is tagged with; anything else is `unknown`."""


def language_code(text: str) -> str:
    """The language an answer to `text` is given in, as a tag value.

    Answers are written in the question's language, so the question decides.
    """
    return LANGUAGE_CODES.get(detect(text), "unknown")


class TracedAnswerService:
    """Implements `AnswerService`: answers through `inner`, then traces the run.

    The tracer's seam, and the outermost answer service in composition, so
    that every answer the chain gives is traced -- the self-description,
    obligations and decisions as well as both reasoning paths -- each named by
    the feature the service that answered it recorded. Catch-up and said-by
    are answered by `AskService` before this chain and exported there, through
    the same `export_run`. Who is exported is still the tracer's decision
    (`OptOutAwareTracer`), not this class's.
    """

    def __init__(self, inner: RunAnswerService, tracer: RunTracer) -> None:
        self._inner = inner
        self._tracer = tracer

    async def answer(self, question: Question) -> Answer:
        return (await self.answer_run(question)).answer

    async def answer_run(self, question: Question) -> RunOutcome:
        outcome = await self._inner.answer_run(question)
        await export_run(self._tracer, question, outcome)
        return outcome


async def export_run(tracer: RunTracer, question: Question, outcome: RunOutcome) -> None:
    """Hand a finished run to `tracer`; never raises.

    The one way a run is traced, shared by `TracedAnswerService` and the routes
    `AskService` answers before the answer chain (catch-up, said-by), so every
    answer to a question is exported the same way.
    """
    # Guarded here as well as in the adapter. `RunTracer` says an
    # implementation must not raise, and the Langfuse one does not -- but
    # "must not" is a comment, and the cost of one being wrong is a person
    # losing their answer to a bookkeeping error.
    try:
        await tracer.trace(
            RunTrace(
                question=question,
                answer=outcome.answer,
                record=outcome.record,
                evidence=outcome.evidence,
                language=language_code(question.text),
            )
        )
    except Exception as exc:  # noqa: BLE001 - tracing never costs a reply
        log.warning("reasoning.trace_failed", error=str(exc))


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

    async def drain_pending(self, batch: int = 100) -> int:
        """`retry_pending` until the queue is empty or a deletion is refused.

        The queue is shared: a retention sweep can mark a backlog of
        thousands at once, and one batch per pass would leave an opt-out or a
        deleted message marked after it waiting days behind that backlog.
        Draining keeps every withdrawal within one pass of being marked.
        """
        total = 0
        while True:
            withdrawn = await self.retry_pending(batch)
            total += withdrawn
            if withdrawn < batch:
                return total

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


@dataclass(frozen=True, slots=True)
class RetentionSweep:
    """What one retention pass marked; None for `found` when Langfuse was unreadable."""

    expired: int
    found: int | None


class TraceRetention:
    """Marks this application's traces older than the retention period for deletion.

    Two sources, because either alone misses traces. The index holds every
    trace exported since it existed; Langfuse also holds traces whose index row
    was never written (the write swallows failures) or that predate the index.
    Deleting is left to `TraceWithdrawal.retry_pending`, so a sweep and a
    deletion fail and retry independently, as the asker search does.
    """

    def __init__(
        self, index: TraceIndex, finder: ExpiredTraceFinder, retention: timedelta
    ) -> None:
        self._index = index
        self._finder = finder
        self._retention = retention

    async def sweep(self, now: datetime) -> RetentionSweep:
        cutoff = now - self._retention
        expired = await self._index.request_deletion_before(cutoff)
        found = await self._finder.find_traces_before(cutoff)
        if found is None:
            log.warning("tracing.retention_search_deferred")
        else:
            await self._index.record_expired_traces(found)
        log.info(
            "tracing.retention_swept",
            expired=len(expired),
            found=None if found is None else len(found),
        )
        return RetentionSweep(len(expired), None if found is None else len(found))
