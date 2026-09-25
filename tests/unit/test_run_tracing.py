"""Tracing: what leaves, what does not, and what it must never cost.

The two properties worth testing are both negative. A trace must never change
an answer, so an unreachable or slow destination is exercised directly; and a
trace must never outlive the content it quotes, so the deletion path is
exercised with the destination both working and down.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import httpx

from chatmemory.adapters.tracing.langfuse import (
    LangfuseTraceDeleter,
    LangfuseTraceFinder,
    LangfuseTracer,
)
from chatmemory.app.reasoning.budgets import Budget
from chatmemory.app.reasoning.contract import (
    AnswerPath,
    RunRecord,
    RunStatus,
    RunTrace,
    TerminalCause,
)
from chatmemory.app.reasoning.fixed import CorrectiveDriver, FixedPath
from chatmemory.app.reasoning.loop import ReasoningLoop
from chatmemory.app.reasoning.service import ReasoningAnswerService
from chatmemory.app.reasoning.tracing import OptOutAwareTracer, TraceWithdrawal
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.answers import Answer
from tests.unit.test_reasoning_fixed import (
    CitingSynthesizer,
    FakePlanner,
    FakeRetrieval,
    ScriptedCritic,
    evidence,
    question,
)


class CapturingTracer:
    def __init__(self) -> None:
        self.traces: list[RunTrace] = []

    async def trace(self, run: RunTrace) -> None:
        self.traces.append(run)


class ExplodingTracer:
    async def trace(self, run: RunTrace) -> None:
        raise RuntimeError("destination is down")


class FakeOptOut:
    def __init__(self, opted_out: set[int] | None = None, raises: bool = False) -> None:
        self._out = opted_out or set()
        self._raises = raises

    async def is_opted_out(self, person: PersonRef) -> bool:
        if self._raises:
            raise RuntimeError("registry unavailable")
        return person.platform_user_id in self._out


def build_service(
    retrieval: FakeRetrieval, tracer: object | None = None
) -> ReasoningAnswerService:
    critic = ScriptedCritic()
    synthesizer = CitingSynthesizer()
    driver = CorrectiveDriver(retrieval, critic, budget=Budget(max_attempts=4))
    return ReasoningAnswerService(
        FixedPath(driver, synthesizer),
        ReasoningLoop(driver, FakePlanner("first", "second"), synthesizer),
        tracer=tracer,  # type: ignore[arg-type]
    )


# --- what a trace carries ---------------------------------------------


async def test_a_trace_carries_the_question_the_answer_and_the_evidence() -> None:
    tracer = CapturingTracer()
    service = build_service(FakeRetrieval([[evidence(1)]]), tracer)
    asked = question(text="what happened in infra yesterday")

    answer = await service.answer(asked)

    assert len(tracer.traces) == 1
    traced = tracer.traces[0]
    assert traced.question.text == asked.text
    assert traced.answer.text == answer.text
    assert traced.evidence, "the evidence behind the answer must reach the trace"
    assert traced.evidence[0].text == evidence(1).text


async def test_the_evidence_survives_the_service_rebuilding_the_outcome() -> None:
    """`_recorded` builds a new RunOutcome. Dropping `evidence` there would
    leave every trace holding an answer with nothing behind it."""
    tracer = CapturingTracer()
    service = build_service(FakeRetrieval([[evidence(1), evidence(2)]]), tracer)

    outcome = await service.answer_run(question(text="what happened in infra"))

    assert outcome.evidence
    assert len(tracer.traces[0].evidence) == len(outcome.evidence)


async def test_no_tracer_configured_exports_nothing_and_still_answers() -> None:
    service = build_service(FakeRetrieval([[evidence(1)]]))
    answer = await service.answer(question(text="what happened in infra"))
    assert answer.text


# --- tracing must never cost an answer --------------------------------


async def test_a_tracer_that_raises_does_not_cost_the_person_their_answer() -> None:
    """Guarded at the service as well as in the adapter: `RunTracer` says an
    implementation must not raise, and a comment is not an enforcement."""
    service = build_service(FakeRetrieval([[evidence(1)]]), ExplodingTracer())
    answer = await service.answer(question(text="what happened in infra"))
    assert answer.text
    assert not answer.abstained


async def test_the_langfuse_adapter_swallows_an_unreachable_destination() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    tracer = LangfuseTracer(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    await tracer.trace(_trace())  # must not raise


async def test_the_langfuse_adapter_swallows_a_rejected_export() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorised"})

    tracer = LangfuseTracer(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        client=httpx.AsyncClient(transport=httpx.MockTransport(reject)),
    )
    await tracer.trace(_trace())  # must not raise


async def test_a_slow_destination_is_abandoned_at_the_timeout() -> None:
    async def crawl(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(207, json={})

    tracer = LangfuseTracer(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        client=httpx.AsyncClient(transport=httpx.MockTransport(crawl), timeout=0.05),
        timeout=0.05,
    )
    await asyncio.wait_for(tracer.trace(_trace()), timeout=2.0)


# --- who is not traced ------------------------------------------------


async def test_an_opted_out_asker_is_never_exported() -> None:
    inner = CapturingTracer()
    tracer = OptOutAwareTracer(inner, FakeOptOut({7}))
    await tracer.trace(_trace(person_id=7))
    assert inner.traces == []


async def test_a_person_who_has_not_opted_out_is_exported() -> None:
    inner = CapturingTracer()
    tracer = OptOutAwareTracer(inner, FakeOptOut({7}))
    await tracer.trace(_trace(person_id=8))
    assert len(inner.traces) == 1


async def test_an_unreadable_optout_registry_withholds_rather_than_exports() -> None:
    """Failing the other way would export on exactly the errors nobody sees."""
    inner = CapturingTracer()
    tracer = OptOutAwareTracer(inner, FakeOptOut(raises=True))
    await tracer.trace(_trace())
    assert inner.traces == []


# --- deletion ---------------------------------------------------------


class FakeIndex:
    def __init__(self, by_message: dict[int, list[str]] | None = None) -> None:
        self.by_message = by_message or {}
        self.requested: list[str] = []
        self.confirmed: list[str] = []
        self.askers: dict[str, int | None] = {}
        self.searches: list[int] = []
        self.found: dict[int, list[str]] = {}

    async def record_export(
        self, trace_id: str, message_ids: list[int], asker: PersonRef | None = None
    ) -> None:
        for m in message_ids:
            self.by_message.setdefault(m, []).append(trace_id)
        self.askers[trace_id] = asker.platform_user_id if asker else None

    async def request_deletion_for_asker(self, platform_user_ids: list[int]) -> list[str]:
        ids = [t for t, a in self.askers.items() if a in platform_user_ids]
        self.requested.extend(ids)
        return ids

    async def open_asker_searches(self, limit: int) -> list[int]:
        return [s for s in self.searches if s not in self.found][:limit]

    async def record_found_traces(
        self, platform_user_id: int, trace_ids: list[str], started: datetime
    ) -> None:
        self.found[platform_user_id] = list(trace_ids)
        self.requested.extend(trace_ids)

    async def request_deletion_for_message(self, message_id: int) -> list[str]:
        ids = list(self.by_message.get(message_id, []))
        self.requested.extend(ids)
        return ids

    async def confirm_deleted(self, trace_ids: list[str]) -> None:
        self.confirmed.extend(trace_ids)

    async def pending_deletions(self, limit: int) -> list[str]:
        return [t for t in self.requested if t not in self.confirmed][:limit]


class FakeDeleter:
    def __init__(self, working: bool = True) -> None:
        self.working = working
        self.deleted: list[str] = []

    async def delete_traces(self, trace_ids: list[str]) -> bool:
        if not self.working:
            return False
        self.deleted.extend(trace_ids)
        return True


async def test_deleting_a_traced_message_deletes_its_trace() -> None:
    index = FakeIndex({42: ["trace-a", "trace-b"]})
    deleter = FakeDeleter()
    await TraceWithdrawal(index, deleter).withdraw_message(42)

    assert deleter.deleted == ["trace-a", "trace-b"]
    assert index.confirmed == ["trace-a", "trace-b"]


async def test_a_message_that_was_never_traced_deletes_nothing() -> None:
    index = FakeIndex()
    deleter = FakeDeleter()
    await TraceWithdrawal(index, deleter).withdraw_message(42)
    assert deleter.deleted == []


async def test_an_unreachable_destination_leaves_the_deletion_pending() -> None:
    index = FakeIndex({42: ["trace-a"]})
    deleter = FakeDeleter(working=False)
    withdrawal = TraceWithdrawal(index, deleter)

    await withdrawal.withdraw_message(42)
    assert index.confirmed == []
    assert await index.pending_deletions(10) == ["trace-a"]

    # ... and the sweep finishes the job once it comes back.
    deleter.working = True
    assert await withdrawal.retry_pending() == 1
    assert deleter.deleted == ["trace-a"]
    assert index.confirmed == ["trace-a"]


async def test_the_deleter_reports_failure_rather_than_raising() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    deleter = LangfuseTraceDeleter(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    assert await deleter.delete_traces(["trace-a"]) is False


async def test_only_corpus_evidence_is_recorded_for_deletion() -> None:
    """A web result carries no message ids and gets no tombstone; recording
    one would leave a deletion chasing something that cannot be deleted."""
    def accept(request: httpx.Request) -> httpx.Response:
        return httpx.Response(207, json={"successes": [], "errors": []})

    index = FakeIndex()
    tracer = LangfuseTracer(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        index=index,  # type: ignore[arg-type]
        client=httpx.AsyncClient(transport=httpx.MockTransport(accept)),
    )
    await tracer.trace(_trace())
    assert set(index.by_message) == {101, 102}


async def test_the_asker_is_recorded_with_the_export() -> None:
    """Without it an opt-out cannot find the traces of the person's questions."""
    def accept(request: httpx.Request) -> httpx.Response:
        return httpx.Response(207, json={"successes": [], "errors": []})

    index = FakeIndex()
    tracer = LangfuseTracer(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        index=index,  # type: ignore[arg-type]
        client=httpx.AsyncClient(transport=httpx.MockTransport(accept)),
    )
    await tracer.trace(_trace(person_id=7))
    assert list(index.askers.values()) == [7]


# --- the Langfuse search backstop ---------------------------------------


def _row(trace_id: str, name: str = "fixed", environment: str = "production") -> dict:
    return {"id": trace_id, "name": name, "environment": environment, "userId": "7"}


def _finder(handler: object) -> LangfuseTraceFinder:
    return LangfuseTraceFinder(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_the_search_returns_only_this_apps_traces_in_this_environment() -> None:
    """A shared project holds other apps' and environments' traces under the
    same user id; the server's filter is not trusted to exclude them."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "data": [
                _row("ours-fixed"),
                _row("ours-loop", name="loop"),
                _row("foreign-env", environment="staging"),
                _row("foreign-name", name="checkout"),
            ],
            "meta": {"page": 1, "totalPages": 1},
        })

    assert await _finder(handle).find_traces_by_user(7) == ["ours-fixed", "ours-loop"]
    [request] = seen
    assert request.method == "GET"
    assert request.url.path == "/api/public/traces"
    assert request.url.params["userId"] == "7"
    assert request.url.params["environment"] == "production"


async def test_the_search_reads_every_page() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={
            "data": [_row(f"trace-{page}")], "meta": {"page": page, "totalPages": 3},
        })

    assert await _finder(handle).find_traces_by_user(7) == [
        "trace-1", "trace-2", "trace-3",
    ]


async def test_a_refused_search_reports_failure_rather_than_raising() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Invalid request data"})

    assert await _finder(refuse).find_traces_by_user(7) is None


class FakeFinder:
    def __init__(self, by_user: dict[int, list[str] | None]) -> None:
        self.by_user = by_user

    async def find_traces_by_user(self, platform_user_id: int) -> list[str] | None:
        return self.by_user.get(platform_user_id, [])


async def test_found_traces_are_marked_and_then_deleted() -> None:
    index = FakeIndex()
    index.searches = [7]
    deleter = FakeDeleter()
    withdrawal = TraceWithdrawal(index, deleter, FakeFinder({7: ["old-a", "old-b"]}))

    assert await withdrawal.search_askers() == 2
    assert await withdrawal.retry_pending() == 2
    assert deleter.deleted == ["old-a", "old-b"]


async def test_a_search_the_destination_refused_stays_open() -> None:
    index = FakeIndex()
    index.searches = [7]
    withdrawal = TraceWithdrawal(index, FakeDeleter(), FakeFinder({7: None}))

    assert await withdrawal.search_askers() == 0
    assert await index.open_asker_searches(10) == [7]


async def test_a_refused_deletion_leaves_the_traces_pending() -> None:
    """A newer Langfuse that answers the DELETE with a 400 has not deleted
    anything; confirming it would lose the deletion for good."""
    bodies: list[dict] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(400, json={"message": "Invalid request data"})

    index = FakeIndex({42: ["trace-a"]})
    deleter = LangfuseTraceDeleter(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        transport=httpx.MockTransport(refuse),
    )
    await TraceWithdrawal(index, deleter).withdraw_message(42)

    assert bodies == [{"traceIds": ["trace-a"]}]
    assert index.confirmed == []
    assert await index.pending_deletions(10) == ["trace-a"]


# --- helpers ----------------------------------------------------------


def _trace(person_id: int = 1) -> RunTrace:
    asked = question(text="what happened in infra")
    asker = asked.asker.__class__(
        person=PersonRef(platform="discord", platform_user_id=person_id),
        visible_channels=asked.asker.visible_channels,
    )
    return RunTrace(
        question=asked.__class__(
            text=asked.text, asker=asker, audience=asked.audience
        ),
        answer=Answer(text="they shipped the migration"),
        record=RunRecord(
            path=AnswerPath.FIXED,
            status=RunStatus.ANSWERED,
            cause=TerminalCause.EVIDENCE_SUFFICIENT,
        ),
        evidence=(
            evidence(1).__class__(
                window_id=1,
                channel=evidence(1).channel,
                text="we shipped the migration",
                score=0.9,
                relevance_source=evidence(1).relevance_source,
                message_ids=(101, 102),
            ),
        ),
    )
