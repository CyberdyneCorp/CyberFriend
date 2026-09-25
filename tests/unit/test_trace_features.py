"""Traces name their feature, carry our tag and environment, and quote nothing.

Tasks 2.1-2.4 and 2.8 (in part) of `add-usage-and-cost-view`:

* every route decision maps to a stable feature id, set where the route is
  decided and never inferred by the exporter;
* the tracer seam is the outermost answer service, so the self-description,
  obligations and decisions are traced as well as both reasoning paths;
* the batch names the trace by its feature and tags it `app:cyberfriend`,
  feature, path, language and tools, in the configured environment;
* evidence and citations leave as references only -- no text, no excerpt;
* an opted-out asker is still never exported.
"""

from __future__ import annotations

import ast
import inspect
import json
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from chatmemory.adapters.tracing.langfuse import (
    APP_TAG,
    TRACE_NAMES,
    LangfuseTraceFinder,
    LangfuseTracer,
    warn_unless_supported,
)
from chatmemory.app.asks.model import to_person
from chatmemory.app.reasoning import features
from chatmemory.app.reasoning.contract import (
    AnswerPath,
    Decision,
    DecisionMaker,
    RunOutcome,
    RunRecord,
    RunStatus,
    RunTrace,
    TerminalCause,
)
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.loop import FEDERATION_CALL
from chatmemory.app.reasoning.service import (
    CHAIN_HANDLERS,
    CORPUS_FEATURES,
    MARKET_FEATURES,
    ReasoningAnswerService,
)
from chatmemory.app.reasoning.tracing import OptOutAwareTracer, TracedAnswerService
from chatmemory.app.routing import MarketKind, Route, RoutingDecision
from chatmemory.app.routing_crypto import CryptoQuery, CryptoRoute
from chatmemory.app.self_description import SelfDescriptionAnswerService
from chatmemory.entrypoints import admin
from chatmemory.ports.answers import Answer, Citation, Question
from tests.unit.test_asks_support import ALICE, BOB, OPEN_CHANNEL, ask, viewer
from tests.unit.test_decision_answers import EARLIER, LATER, FakeDecisions
from tests.unit.test_decision_answers import question as decision_question
from tests.unit.test_decision_answers import service as decision_service
from tests.unit.test_obligations_wiring import RecordingAnswers, store_with
from tests.unit.test_obligations_wiring import question as obligation_question
from tests.unit.test_obligations_wiring import service as obligation_service
from tests.unit.test_reasoning_fixed import evidence, question
from tests.unit.test_run_tracing import CapturingTracer, FakeIndex, FakeOptOut

ADDRESS = "0x" + "ab" * 20
EVIDENCE_TEXT = "Ana in #leadership: the layoffs are planned for Friday"
CITED_EXCERPT = "the layoffs are planned"


class StubPath:
    """Both reasoning paths, answering from nowhere, and counting runs."""

    def __init__(self, status: RunStatus = RunStatus.ANSWERED, outside: bool = False) -> None:
        self.status = status
        self.can_reach_outside = outside
        self.runs = 0

    def _outcome(self, path: AnswerPath, window_id: int = 1) -> RunOutcome:
        return RunOutcome(
            answer=Answer("stub", consulted_channels=frozenset()),
            record=RunRecord(
                path=path,
                status=self.status,
                cause=TerminalCause.EVIDENCE_SUFFICIENT,
                evidence_window_ids=(window_id,),
            ),
        )

    async def run(self, question: Question, consult_outside: bool = True) -> RunOutcome:
        self.runs += 1
        # An escalated run holds outside evidence (a negative window id).
        return self._outcome(AnswerPath.LOOP, -1 if consult_outside else 1)

    async def run_external(self, question: Question, *_: Any, **__: Any) -> RunOutcome:
        return self._outcome(AnswerPath.LOOP, -1)


def reasoning(
    route: Route = Route.FIXED,
    fixed_status: RunStatus = RunStatus.ANSWERED,
    outside: bool = False,
) -> ReasoningAnswerService:
    def classifier(_: str) -> RoutingDecision:
        return RoutingDecision(route, frozenset())

    return ReasoningAnswerService(
        StubPath(fixed_status),  # type: ignore[arg-type]
        StubPath(outside=outside),  # type: ignore[arg-type]
        classifier=classifier,
    )


# --- 2.1 every route decision maps to a feature --------------------------


def test_every_route_kind_has_a_feature() -> None:
    """Structural: a new route kind without a feature fails here, not in Langfuse."""
    assert set(MARKET_FEATURES) == set(MarketKind)
    assert set(CORPUS_FEATURES) == set(Route)
    assert set(CHAIN_HANDLERS) == set(CryptoRoute)
    named = {
        *MARKET_FEATURES.values(),
        *CORPUS_FEATURES.values(),
        *(h.feature for h in CHAIN_HANDLERS.values()),
    }
    assert named <= features.FEATURES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("please add a new MCP server for jira", features.FEDERATION),
        ("what is BTC at", features.MARKET_PRICE),
        ("where is the S&P 500 today", features.MARKET_PRICE),
        ("how much is 100 USD in BRL", features.MARKET_OTHER),
        ("what time is it", features.TIME),
        ("search the web for the latest Python release", features.WEB_SEARCH),
        (f"what is the balance of {ADDRESS}", features.WALLET_BALANCE),
        ("what's my wallet balance", features.WALLET_BALANCE),
        ("what happened in infra yesterday", features.CORPUS_FIXED),
    ],
)
async def test_each_route_names_its_feature(text: str, expected: str) -> None:
    outcome = await reasoning().answer_run(question(text=text))
    assert outcome.record.feature == expected


async def test_the_loop_route_is_named_corpus_loop() -> None:
    outcome = await reasoning(Route.LOOP).answer_run(question(text="what happened in infra"))
    assert outcome.record.feature == features.CORPUS_LOOP


@pytest.mark.parametrize("route", list(CryptoRoute))
@pytest.mark.parametrize("addresses", [(ADDRESS,), ()])
async def test_every_chain_route_names_its_feature_with_or_without_an_address(
    route: CryptoRoute, addresses: tuple[str, ...]
) -> None:
    """With no address the reply asks for one -- still counted as that feature."""
    service = reasoning()
    _, outcome = await service._chain_route(  # noqa: SLF001
        question(text="wallet question"), CryptoQuery(route=route, addresses=addresses)
    )
    assert outcome.record.feature == CHAIN_HANDLERS[route].feature


async def test_an_escalated_answer_is_named_federation() -> None:
    service = reasoning(fixed_status=RunStatus.ABSTAINED, outside=True)
    outcome = await service.answer_run(question(text="who wrote the novel Dune"))
    assert outcome.record.feature == features.FEDERATION


async def test_an_escalation_that_is_not_used_keeps_the_corpus_feature() -> None:
    service = reasoning(fixed_status=RunStatus.ABSTAINED, outside=False)
    outcome = await service.answer_run(question(text="who wrote the novel Dune"))
    assert outcome.record.feature == features.CORPUS_FIXED


# --- 2.4 the seam is the outermost answer service ------------------------


async def test_a_capabilities_question_is_traced_as_capabilities() -> None:
    tracer = CapturingTracer()
    front = TracedAnswerService(SelfDescriptionAnswerService(reasoning()), tracer)

    answer = await front.answer(question(text="what can you do?"))

    [traced] = tracer.traces
    assert traced.record.feature == features.CAPABILITIES
    assert traced.answer.text == answer.text


async def test_an_obligation_question_is_traced_as_obligations() -> None:
    tracer = CapturingTracer()
    store = store_with(ask(source_message_id=10, requester=ALICE, addressee=to_person(BOB)))
    front = TracedAnswerService(
        obligation_service(store, RecordingAnswers()),  # type: ignore[arg-type]
        tracer,
    )

    await front.answer(
        obligation_question("what did people ask me today", viewer(BOB, OPEN_CHANNEL), OPEN_CHANNEL)
    )

    [traced] = tracer.traces
    assert traced.record.feature == features.OBLIGATIONS
    assert [c.message_id for c in traced.answer.citations] == [10]


async def test_a_decision_question_is_traced_as_decisions() -> None:
    tracer = CapturingTracer()
    answers, _, _ = decision_service(FakeDecisions([LATER, EARLIER]))
    front = TracedAnswerService(answers, tracer)

    await front.answer(decision_question("o que decidimos sobre o deploy?"))

    [traced] = tracer.traces
    assert traced.record.feature == features.DECISIONS
    assert traced.language == "pt"


async def test_a_question_the_wrappers_pass_on_is_traced_once_with_the_reasoning_feature() -> None:
    tracer = CapturingTracer()
    front = TracedAnswerService(SelfDescriptionAnswerService(reasoning()), tracer)

    await front.answer(question(text="what time is it"))

    assert [t.record.feature for t in tracer.traces] == [features.TIME]


async def test_the_reasoning_service_no_longer_traces_by_itself() -> None:
    """One seam: a second one would export every reasoning run twice."""
    assert "tracer" not in ReasoningAnswerService.__init__.__code__.co_varnames


async def test_a_tracer_that_raises_at_the_seam_does_not_cost_the_answer() -> None:
    class Exploding:
        async def trace(self, run: RunTrace) -> None:
            raise RuntimeError("destination is down")

    front = TracedAnswerService(SelfDescriptionAnswerService(reasoning()), Exploding())
    answer = await front.answer(question(text="what can you do?"))
    assert "CyberFriend" in answer.text


async def test_an_opted_out_asker_is_still_not_exported_through_the_seam() -> None:
    inner = CapturingTracer()
    asked = question(text="what can you do?")
    opted_out = FakeOptOut({asked.asker.person.platform_user_id})
    front = TracedAnswerService(
        SelfDescriptionAnswerService(reasoning()), OptOutAwareTracer(inner, opted_out)
    )

    assert (await front.answer(asked)).text
    assert inner.traces == []


# --- 2.2 / 2.3 what the batch carries ------------------------------------


def _capture() -> tuple[list[dict[str, Any]], httpx.AsyncClient]:
    sent: list[dict[str, Any]] = []

    def accept(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(207, json={"successes": [], "errors": []})

    return sent, httpx.AsyncClient(transport=httpx.MockTransport(accept))


def _run(feature: str = features.CORPUS_LOOP) -> RunTrace:
    base = evidence(1)
    held = Evidence(
        window_id=7,
        channel=base.channel,
        text=EVIDENCE_TEXT,
        score=0.8123456,
        relevance_source=base.relevance_source,
        message_ids=(501,),
        author_display="Ana",
        url="https://discord.example/501",
    )
    cited = Citation(
        channel=base.channel,
        message_id=502,
        author_display="Ana",
        excerpt=CITED_EXCERPT,
        url="https://discord.example/502",
    )
    record = RunRecord(
        path=AnswerPath.LOOP,
        status=RunStatus.ANSWERED,
        cause=TerminalCause.EVIDENCE_SUFFICIENT,
        feature=feature,
        decisions=(
            Decision(
                FEDERATION_CALL,
                "tool_requested",
                DecisionMaker.MODEL,
                model_calls=1,
                detail="serpapi:search",
            ),
        ),
    )
    return RunTrace(
        question=question(text="when is the offsite?"),
        answer=Answer("The offsite is on Friday.", citations=(cited,)),
        record=record,
        evidence=(held,),
        language="en",
    )


async def _exported(run: RunTrace, index: FakeIndex | None = None) -> dict[str, Any]:
    sent, client = _capture()
    tracer = LangfuseTracer(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        index=index,  # type: ignore[arg-type]
        client=client,
        environment="staging",
    )
    await tracer.trace(run)
    [payload] = sent
    return payload


async def test_the_trace_is_named_by_its_feature_and_tagged_for_this_app() -> None:
    payload = await _exported(_run())
    [event] = payload["batch"]
    body = event["body"]

    assert event["type"] == "trace-create"
    assert body["name"] == features.CORPUS_LOOP
    assert body["environment"] == "staging"
    assert body["tags"] == [
        APP_TAG,
        f"feature:{features.CORPUS_LOOP}",
        "path:loop",
        "lang:en",
        "tool:serpapi:search",
    ]
    assert body["metadata"]["path"] == "loop"


async def test_no_evidence_text_or_excerpt_is_in_the_batch() -> None:
    payload = await _exported(_run())
    dumped = json.dumps(payload)

    assert EVIDENCE_TEXT not in dumped
    assert CITED_EXCERPT not in dumped
    assert "Ana" not in dumped, "an author name is part of what the evidence says"
    metadata = payload["batch"][0]["body"]["metadata"]
    assert metadata["evidence"] == [
        {"window_id": 7, "channel": str(evidence(1).channel), "source_system": "discord",
         "score": 0.8123}
    ]
    assert metadata["citations"] == [
        {"channel": str(evidence(1).channel), "message_id": 502, "source_system": "discord"}
    ]


async def test_the_messages_a_trace_draws_on_are_still_indexed() -> None:
    """References only in the trace, but deleting a message must still withdraw
    it -- including one only a citation points at (an obligation's source)."""
    index = FakeIndex()
    await _exported(_run(), index)
    assert set(index.by_message) == {501, 502}


async def test_an_unnamed_record_falls_back_to_its_path() -> None:
    payload = await _exported(_run(feature=""))
    body = payload["batch"][0]["body"]
    assert body["name"] == "loop"
    assert f"feature:{AnswerPath.LOOP}" in body["tags"]


async def test_the_search_backstop_finds_feature_named_traces() -> None:
    """A trace is now named by its feature, and an opt-out's search must still
    recognise it as ours -- as it does the path names of older traces."""
    assert features.FEATURES <= TRACE_NAMES
    assert {"fixed", "loop"} <= TRACE_NAMES

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [
                {"id": "new", "name": features.MARKET_PRICE, "environment": "production"},
                {"id": "old", "name": "fixed", "environment": "production"},
                {"id": "foreign", "name": "checkout", "environment": "production"},
            ],
            "meta": {"page": 1, "totalPages": 1},
        })

    finder = LangfuseTraceFinder(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        transport=httpx.MockTransport(handle),
    )
    assert await finder.find_traces_by_user(7) == ["new", "old"]


# --- ops 1.2 the v4 trap is reported at startup ----------------------------


def _health(version: str | None, status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/public/health"
        body = {"status": "OK"} if version is None else {"status": "OK", "version": version}
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("version", "status", "major", "warned"),
    [
        ("3.225.8", 200, 3, None),
        ("4.0.1", 200, 4, "tracing.langfuse_unsupported_version"),
        (None, 200, None, "tracing.langfuse_unsupported_version"),
        ("3.225.8", 503, None, "tracing.langfuse_health_unreadable"),
    ],
)
async def test_a_langfuse_other_than_v3_is_warned_about_and_never_fatal(
    version: str | None, status: int, major: int | None, warned: str | None
) -> None:
    with capture_logs() as logs:
        seen = await warn_unless_supported(
            "http://langfuse.invalid", transport=_health(version, status)
        )
    assert seen == major
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == (
        [warned] if warned else []
    )


def test_the_admin_process_checks_the_langfuse_version_at_startup() -> None:
    main = ast.parse(inspect.getsource(admin.main))
    called = {
        getattr(node.func, "id", None)
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
    }
    assert "warn_unless_supported" in called
