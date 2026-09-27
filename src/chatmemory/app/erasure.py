"""Delete everything held about a person, and finish even if interrupted.

`/privacy` -> [Delete everything...] runs `ErasureService.erase`. The steps,
numbered as `ErasureStep` numbers them:

1.  Record the request (`erasure_request`), with the counts the reply reports.
2.  Stop re-import before anything is purged: `person.erased_before` is set to
    the request time, and the database drops any message, mention or document
    entry of theirs created before it, whatever issues the write. With the
    second button the person is opted out too, which also runs
    `purge_person_derived` in the same transaction.
3-4. Mark every trace quoting their messages and every trace of their
    questions for deletion, and queue the Langfuse search for traces exported
    before the asker was recorded (`OptOutService.withdraw_traces`). Before
    the purge, because the message rows are what say who wrote a quote.
    Marking, not deleting: the ingest process's withdrawal sweep deletes, and
    retries while Langfuse is down.
5.  Purge their messages and everything built on them, and their documents
    (`OptOutService.purge_contributions`), then `purge_person_derived`. The
    same path an admin opt-out takes, so the two can never drift apart.
6.  Fold their voice seconds into the anonymous monthly total, so the
    server-wide ceiling does not change.
7.  Reduce the person row to a tombstone: id, platform ids, `erased_before`,
    and the opt-out flag if they chose it. Name and preferences cleared.
8.  Mark the request complete.

Each step is idempotent and the request records the last one that finished,
so `resume` can pick up from any point: the bot runs the steps at once, and a
sweep in the ingest process resumes a request nobody has advanced for a while
(the process that started it crashed or was redeployed).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog

from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.privacy import (
    ErasureCounts,
    ErasureMode,
    ErasureRequest,
    ErasureStep,
    ErasureStore,
)

log = structlog.get_logger()

OPT_OUT_REASON = "self-service erasure"
"""What `person_opt_out.reason` says for the second button."""

IDLE_BEFORE_RESUME = timedelta(minutes=2)
"""How long a request goes without a step finishing before the sweep takes it
over. Longer than any one step takes, so the sweep does not race the bot."""

RESUME_BATCH = 20


class ErasureService:
    def __init__(
        self, store: ErasureStore, optouts: OptOutService, clock: Clock = utc_now
    ) -> None:
        self._store = store
        self._optouts = optouts
        self._clock = clock
        self._steps: tuple[tuple[ErasureStep, Callable[[ErasureRequest], Awaitable[None]]], ...] = (
            (ErasureStep.REIMPORT_STOPPED, self._stop_reimport),
            (ErasureStep.TRACES_MARKED, self._withdraw_traces),
            (ErasureStep.PURGED, self._purge),
            (ErasureStep.VOICE_FOLDED, self._fold_voice),
            (ErasureStep.TOMBSTONED, self._store.tombstone),
        )

    async def erase(
        self, person: PersonRef, mode: ErasureMode, counts: ErasureCounts
    ) -> ErasureRequest:
        """Record the request, then run every step. Returns the finished request."""
        request = await self._store.open(person, mode, counts)
        log.info("erasure.requested", request_id=request.id, mode=mode.value)
        return await self.resume(request)

    async def resume(self, request: ErasureRequest) -> ErasureRequest:
        """Run the steps after `request.step`, recording each as it finishes."""
        for step, run in self._steps:
            if request.step >= step:
                continue
            await run(request)
            request = await self._store.advance(request, step)
        if not request.complete:
            request = await self._store.advance(request, ErasureStep.COMPLETE)
            log.info("erasure.completed", request_id=request.id, mode=request.mode.value)
        return request

    async def resume_stalled(self) -> int:
        """Finish requests nobody has advanced for `IDLE_BEFORE_RESUME`. For the
        ingest sweep. One failing request does not hold up the others."""
        stalled = await self._store.open_requests(
            self._clock() - IDLE_BEFORE_RESUME, RESUME_BATCH
        )
        finished = 0
        for request in stalled:
            try:
                await self.resume(request)
                finished += 1
            except Exception:  # noqa: BLE001 - the next pass retries this one
                log.exception("erasure.resume_failed", request_id=request.id)
        return finished

    async def _stop_reimport(self, request: ErasureRequest) -> None:
        await self._store.stop_reimport(request)
        if request.mode is ErasureMode.ERASE_AND_OPT_OUT:
            await self._optouts.exclude(request.person, OPT_OUT_REASON)

    async def _withdraw_traces(self, request: ErasureRequest) -> None:
        traces = await self._optouts.withdraw_traces(request.person)
        await self._store.record_traces(request, traces)

    async def _purge(self, request: ErasureRequest) -> None:
        await self._optouts.purge_contributions(request.person)
        await self._store.purge_derived(request)

    async def _fold_voice(self, request: ErasureRequest) -> None:
        await self._store.fold_voice(request)
