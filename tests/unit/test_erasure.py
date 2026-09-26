"""`ErasureService`: the step order, resuming from any step, and the reply.

Re-import stops before anything is purged; traces are marked before the
messages that say who wrote a quote are deleted; each finished step is
recorded, so a resumed request repeats only the step it stopped in. The reply
gives counts, says traces are scheduled for deletion, and says what happens
from now on for each choice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from chatmemory.adapters.discord.privacy import confirm_word, erasure_reply
from chatmemory.app.erasure import IDLE_BEFORE_RESUME, OPT_OUT_REASON, ErasureService
from chatmemory.app.language import Language
from chatmemory.app.optout import OptOutService, PersonPurge
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.privacy import (
    ErasureCounts,
    ErasureMode,
    ErasureRequest,
    ErasureStep,
)

ALICE = PersonRef("discord", 42)
NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
EN, PT = Language.ENGLISH, Language.PORTUGUESE


class FakeErasureStore:
    """Keeps one request, and the order steps touched it in `log`."""

    def __init__(self, log: list[str], step: ErasureStep = ErasureStep.RECORDED) -> None:
        self.log = log
        self.request = ErasureRequest(
            id=1,
            person_id=7,
            person=ALICE,
            mode=ErasureMode.ERASE,
            step=step,
            requested_at=NOW,
        )
        self.idle_since: datetime | None = None

    async def open(
        self, person: PersonRef, mode: ErasureMode, counts: ErasureCounts
    ) -> ErasureRequest:
        self.request = replace(self.request, person=person, mode=mode, counts=counts)
        return self.request

    async def stop_reimport(self, request: ErasureRequest) -> None:
        self.log.append("stop_reimport")

    async def record_traces(self, request: ErasureRequest, traces: int) -> None:
        self.request = replace(self.request, counts=replace(self.request.counts, traces=traces))

    async def purge_derived(self, request: ErasureRequest) -> None:
        self.log.append("purge_derived")

    async def fold_voice(self, request: ErasureRequest) -> int:
        self.log.append("fold_voice")
        return 90

    async def tombstone(self, request: ErasureRequest) -> None:
        self.log.append("tombstone")

    async def advance(self, request: ErasureRequest, step: ErasureStep) -> ErasureRequest:
        self.log.append(f"step {int(step)}")
        self.request = replace(self.request, step=max(self.request.step, step))
        return self.request

    async def open_requests(self, idle_since: datetime, limit: int) -> Sequence[ErasureRequest]:
        self.idle_since = idle_since
        return [] if self.request.complete else [self.request]


class FakeOptOuts(OptOutService):
    """The three opt-out steps, recorded; `fail_purge` dies in step 5 once."""

    def __init__(self, log: list[str], fail_purge: bool = False) -> None:
        self.log = log
        self.fail_purge = fail_purge

    async def exclude(self, person: PersonRef, reason: str = "") -> None:
        self.log.append(f"exclude:{reason}")

    async def withdraw_traces(self, person: PersonRef) -> int:
        self.log.append("withdraw_traces")
        return 3

    async def purge_contributions(self, person: PersonRef) -> tuple[PersonPurge, int]:
        if self.fail_purge:
            self.fail_purge = False
            raise RuntimeError("killed")
        self.log.append("purge_contributions")
        return PersonPurge(messages=2), 0


def _service(
    log: list[str], store: FakeErasureStore | None = None, fail_purge: bool = False
) -> tuple[ErasureService, FakeErasureStore]:
    store = store or FakeErasureStore(log)
    return ErasureService(store, FakeOptOuts(log, fail_purge), clock=lambda: NOW), store


async def test_reimport_stops_first_and_traces_are_marked_before_the_purge() -> None:
    log: list[str] = []
    service, _ = _service(log)

    done = await service.erase(ALICE, ErasureMode.ERASE, ErasureCounts(messages=2))

    assert log == [
        "stop_reimport", "step 2",
        "withdraw_traces", "step 4",
        "purge_contributions", "purge_derived", "step 5",
        "fold_voice", "step 6",
        "tombstone", "step 7",
        "step 8",
    ]
    assert done.complete and done.counts == ErasureCounts(messages=2, traces=3)


async def test_leaving_opts_the_person_out_in_the_reimport_step() -> None:
    log: list[str] = []
    service, _ = _service(log)

    await service.erase(ALICE, ErasureMode.ERASE_AND_OPT_OUT, ErasureCounts())

    assert log[:3] == ["stop_reimport", f"exclude:{OPT_OUT_REASON}", "step 2"]


async def test_staying_never_opts_the_person_out() -> None:
    log: list[str] = []
    service, _ = _service(log)

    await service.erase(ALICE, ErasureMode.ERASE, ErasureCounts())

    assert not any(entry.startswith("exclude") for entry in log)


async def test_a_resumed_request_repeats_only_what_had_not_finished() -> None:
    log: list[str] = []
    store = FakeErasureStore(log, step=ErasureStep.TRACES_MARKED)
    service, _ = _service(log, store)

    await service.resume(store.request)

    assert "stop_reimport" not in log and "withdraw_traces" not in log
    assert log[0] == "purge_contributions"


async def test_a_crash_leaves_the_request_at_the_last_finished_step() -> None:
    log: list[str] = []
    service, store = _service(log, fail_purge=True)

    with pytest.raises(RuntimeError):
        await service.erase(ALICE, ErasureMode.ERASE, ErasureCounts())

    assert store.request.step is ErasureStep.TRACES_MARKED
    assert await service.resume_stalled() == 1
    assert store.request.complete
    assert store.idle_since == NOW - IDLE_BEFORE_RESUME


async def test_one_failing_request_does_not_stop_the_sweep() -> None:
    log: list[str] = []
    service, store = _service(log, fail_purge=True)
    store.request = replace(store.request, step=ErasureStep.TRACES_MARKED)

    assert await service.resume_stalled() == 0
    assert not store.request.complete


# --- the reply ---------------------------------------------------------------------

DONE = ErasureRequest(
    id=1,
    person_id=7,
    person=ALICE,
    mode=ErasureMode.ERASE,
    step=ErasureStep.COMPLETE,
    requested_at=NOW - timedelta(seconds=1),
    counts=ErasureCounts(
        messages=12, media=3, facts=4, memory=10, tasks=1, alerts=2, suggestions=5, tokens=1,
        traces=6,
    ),
)


def test_the_reply_gives_counts_and_says_traces_are_scheduled() -> None:
    reply = erasure_reply(DONE, EN)

    assert "Messages in channels you can read: 12, and any of yours in other channels" in reply
    assert "Attachments on them: 3" in reply
    assert "Personal details: 4" in reply
    assert "Remembered questions, answers and summaries: 10" in reply
    assert "Scheduled questions: 1" in reply and "Alerts: 2" in reply
    assert "Suggestions: 5" in reply and "Access tokens: 1" in reply
    assert "anonymous monthly total" in reply
    assert "scheduled for deletion from the trace store: at least 6." in reply


def test_staying_says_new_messages_are_archived() -> None:
    assert "New messages are archived as usual" in erasure_reply(DONE, EN)
    assert "stopped archiving" not in erasure_reply(DONE, EN)


def test_leaving_says_archiving_stopped() -> None:
    reply = erasure_reply(replace(DONE, mode=ErasureMode.ERASE_AND_OPT_OUT), EN)

    assert "stopped archiving your messages" in reply
    assert "archived as usual" not in reply


def test_the_reply_and_the_word_are_in_portuguese_for_a_portuguese_caller() -> None:
    reply = erasure_reply(DONE, PT)

    assert reply.startswith("Pronto. Eu apaguei:")
    assert "agendadas para exclusão" in reply
    assert confirm_word(PT) == "APAGAR" and confirm_word(EN) == "DELETE"
