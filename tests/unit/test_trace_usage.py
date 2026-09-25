"""Traces carry per-call model usage and federated tool spans.

Tasks 2.5-2.8 of `add-usage-and-cost-view`:

* `BudgetLedger` records a `ModelUsage` (stage, model, input and output tokens,
  start and end) per model call, and completion tokens survive `Plan`,
  `Grounded` and `Assessment`, which used to drop them;
* each model call leaves as a `generation-create` and each federated tool call
  as a `span-create`, in the same batch as the trace;
* tool arguments are never exported;
* an opted-out asker is still never exported;
* the model price sync is idempotent and never touches Langfuse's own models.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from chatmemory.adapters.tracing.langfuse import LangfuseTracer
from chatmemory.adapters.tracing.langfuse_models import (
    LangfuseModelSync,
    ModelPrice,
    load_price_table,
)
from chatmemory.app.reasoning.budgets import Budget, BudgetLedger, ModelUsage, Spend, ToolUsage
from chatmemory.app.reasoning.contract import (
    AnswerPath,
    RunRecord,
    RunStatus,
    RunTrace,
    TerminalCause,
)
from chatmemory.app.reasoning.evidence import EvidenceLedger
from chatmemory.app.reasoning.fixed import SUFFICIENCY, SYNTHESIS_STAGE, write_answer
from chatmemory.app.reasoning.loop import FEDERATION_CALL, PLAN_STAGE, ToolOutcome
from chatmemory.app.reasoning.ports import (
    JsonCompletion,
    Plan,
    PromptContext,
    TextCompletion,
    ToolCall,
    ToolCompletion,
    ToolDefinition,
)
from chatmemory.app.reasoning.stages import ModelCritic, ModelPlanner, ModelSynthesizer
from chatmemory.app.reasoning.tracing import OptOutAwareTracer
from chatmemory.domain.search import SearchQuery
from chatmemory.ports.answers import Answer
from tests.unit.test_loop_invocation import ISSUES, ScriptedSurface, build_loop
from tests.unit.test_reasoning_fixed import FakePlanner, evidence, question
from tests.unit.test_run_tracing import CapturingTracer, FakeOptOut
from tests.unit.test_tool_calling import ScriptedCompletions, a_response
from tests.unit.test_tool_calling import build as build_chat

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
WALLET = "0x" + "cd" * 20
TABLE = Path(__file__).parents[2] / "scripts" / "langfuse_model_prices.json"


class Ticking:
    """A wall clock that moves a second each time it is read."""

    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at


class JsonChat:
    """A `ChatModel` whose structured answers name a model and spend tokens."""

    def __init__(self, data: Mapping[str, object]) -> None:
        self._data = data

    async def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], schema_name: str
    ) -> JsonCompletion:
        return JsonCompletion(
            data=self._data, prompt_tokens=120, completion_tokens=34, model="gpt-4o"
        )

    async def complete_text(self, system: str, user: str) -> TextCompletion:
        raise AssertionError("not used")

    async def complete_with_tools(
        self, system: str, user: str, tools: Sequence[ToolDefinition]
    ) -> ToolCompletion:
        raise AssertionError("not used")


class CostedPlanner(FakePlanner):
    """A planner whose plan names its model and spends output tokens."""

    async def plan(self, question_text: str, max_steps: int, context: PromptContext) -> Plan:
        return replace(
            await super().plan(question_text, max_steps, context),
            completion_tokens=7,
            model="planner-v1",
        )


# --- 2.5 the ledger and the stages -------------------------------------


def test_the_ledger_records_one_usage_per_model_call() -> None:
    wall = Ticking()
    ledger = BudgetLedger(Budget(), wall=wall)
    started = ledger.now()
    ledger.charge_model_call(
        100, stage="plan", model="gpt-4o", completion_tokens=20, started_at=started
    )

    spend = ledger.spend()
    assert (spend.prompt_tokens, spend.completion_tokens, spend.model_calls) == (100, 20, 1)
    assert spend.models == (
        ModelUsage("plan", "gpt-4o", 100, 20, T0 + timedelta(seconds=1),
                   T0 + timedelta(seconds=2)),
    )


def test_a_stage_answered_without_a_model_leaves_no_usage() -> None:
    ledger = BudgetLedger(Budget())
    ledger.charge_model_call(0, 0, stage=SUFFICIENCY)
    assert ledger.spend().models == ()
    assert ledger.spend().model_calls == 0


def test_a_tool_call_is_recorded_by_name_and_outcome_only() -> None:
    wall = Ticking()
    ledger = BudgetLedger(Budget(), wall=wall)
    ledger.record_tool("issues:search", "call_failed", failed=True, started_at=ledger.now())
    assert ledger.spend().tools == (
        ToolUsage("issues:search", "call_failed", True, T0 + timedelta(seconds=1),
                  T0 + timedelta(seconds=2)),
    )


async def test_the_planner_keeps_completion_tokens_and_model() -> None:
    plan = await ModelPlanner(JsonChat({"sub_questions": ["a"]})).plan("q", 3)
    assert (plan.prompt_tokens, plan.completion_tokens, plan.model) == (120, 34, "gpt-4o")


async def test_the_synthesizer_keeps_completion_tokens_and_model() -> None:
    grounded = await ModelSynthesizer(
        JsonChat({"text": "t", "cited_window_ids": [1]})
    ).synthesize("q", [evidence(1)])
    assert (grounded.completion_tokens, grounded.model) == (34, "gpt-4o")


async def test_the_critic_keeps_completion_tokens_and_model() -> None:
    assessment = await ModelCritic(
        JsonChat({"verdict": "sufficient", "score": 0.9, "suggested_query": ""})
    ).assess("q", SearchQuery(text="q"), [evidence(1)])
    assert (assessment.completion_tokens, assessment.model) == (34, "gpt-4o")


async def test_the_chat_adapter_names_its_model_on_every_completion() -> None:
    chat = build_chat(ScriptedCompletions(a_response(content="{}")), model="chat-v1")
    text = await chat.complete_text("s", "u")
    tools = await chat.complete_with_tools("s", "u", [])
    structured = await chat.complete_json("s", "u", {"type": "object"}, "plan")
    assert (text.model, text.completion_tokens) == ("chat-v1", 12)
    assert (tools.model, tools.completion_tokens) == ("chat-v1", 12)
    # Every plan, sufficiency and synthesis call goes through this path.
    assert (structured.model, structured.completion_tokens) == ("chat-v1", 12)


async def test_write_answer_records_the_synthesis_call_with_its_completion_tokens() -> None:
    """Regression: `Grounded` dropped completion tokens, so no cost could be computed."""
    held = EvidenceLedger()
    held.add([evidence(1)], "discord")
    ledger = BudgetLedger(Budget())
    await write_answer(
        ModelSynthesizer(JsonChat({"text": "t", "cited_window_ids": [1]})),
        question(),
        held,
        ledger,
        [],
        partial=False,
    )
    (usage,) = ledger.spend().models
    assert (usage.stage, usage.model, usage.input_tokens, usage.output_tokens) == (
        SYNTHESIS_STAGE, "gpt-4o", 120, 34,
    )


async def test_a_loop_run_records_every_model_call_and_the_tool_span() -> None:
    wants = ToolCompletion(
        call=ToolCall(name="issues:search", arguments={"query": WALLET}),
        prompt_tokens=40,
        completion_tokens=9,
        model="chat-v1",
    )
    outcome = await build_loop(
        ScriptedSurface(ISSUES, completion=wants), planner=CostedPlanner("what is in the tracker")
    ).run(question())

    spend = outcome.record.spend
    stages = [u.stage for u in spend.models]
    assert stages[:2] == [FEDERATION_CALL, PLAN_STAGE]
    assert stages[-1] == SYNTHESIS_STAGE
    assert spend.models[0].model == "chat-v1"
    assert spend.models[0].output_tokens == 9
    plan = spend.models[1]
    assert (plan.model, plan.input_tokens, plan.output_tokens) == ("planner-v1", 50, 7)
    assert [(t.name, t.outcome, t.failed) for t in spend.tools] == [
        ("issues:search", "invoked", False)
    ]


async def test_a_refused_tool_call_is_a_failed_span() -> None:
    refused = ToolOutcome(invoked=False, source_system="issues", detail="refused")
    outcome = await build_loop(ScriptedSurface(ISSUES, outcome=refused)).run(question())
    assert [(t.outcome, t.failed) for t in outcome.record.spend.tools] == [
        ("not_invoked", True)
    ]


# --- 2.6 / 2.8 what the batch carries ------------------------------------


def _capture() -> tuple[list[dict[str, Any]], httpx.AsyncClient]:
    sent: list[dict[str, Any]] = []

    def accept(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(207, json={"successes": [], "errors": []})

    return sent, httpx.AsyncClient(transport=httpx.MockTransport(accept))


def _trace(spend: Spend) -> RunTrace:
    return RunTrace(
        question=question(text=f"what is in wallet {WALLET}"),
        answer=Answer(text="some tokens"),
        record=RunRecord(
            path=AnswerPath.LOOP,
            status=RunStatus.ANSWERED,
            cause=TerminalCause.EVIDENCE_SUFFICIENT,
            spend=spend,
            feature="wallet.balance",
        ),
    )


SPEND = Spend(
    model_calls=2,
    tool_calls=1,
    prompt_tokens=300,
    completion_tokens=50,
    models=(
        ModelUsage("plan", "gpt-4o", 100, 20, T0, T0 + timedelta(seconds=1)),
        ModelUsage("synthesis", "chat-v1", 200, 30, T0, T0 + timedelta(seconds=2)),
    ),
    tools=(
        ToolUsage("chain:balance", "invoked", False, T0, T0 + timedelta(seconds=3)),
        ToolUsage("chain:activity", "call_failed", True, T0, T0 + timedelta(seconds=4)),
    ),
)


async def _batch(spend: Spend) -> list[dict[str, Any]]:
    sent, client = _capture()
    await LangfuseTracer("http://lf.invalid", "pk", "sk", client=client).trace(_trace(spend))
    assert len(sent) == 1, "generations and spans ride in the trace's own request"
    events: list[dict[str, Any]] = sent[0]["batch"]
    return events


async def test_the_batch_holds_the_trace_a_generation_per_call_and_a_span_per_tool() -> None:
    events = await _batch(SPEND)

    assert [e["type"] for e in events] == [
        "trace-create",
        "generation-create",
        "generation-create",
        "span-create",
        "span-create",
    ]
    trace_id = events[0]["body"]["id"]
    assert all(e["body"]["traceId"] == trace_id for e in events[1:])
    assert events[0]["body"]["metadata"]["completion_tokens"] == 50

    plan, synthesis = (e["body"] for e in events[1:3])
    assert plan["name"] == "plan"
    assert plan["model"] == "gpt-4o"
    assert plan["usageDetails"] == {"input": 100, "output": 20}
    assert plan["startTime"] == T0.isoformat()
    assert synthesis["model"] == "chat-v1"
    assert synthesis["usageDetails"] == {"input": 200, "output": 30}

    ok, failed = (e["body"] for e in events[3:])
    assert (ok["name"], ok["level"], ok["statusMessage"]) == (
        "chain:balance", "DEFAULT", "invoked"
    )
    assert ok["endTime"] == (T0 + timedelta(seconds=3)).isoformat()
    assert (failed["level"], failed["statusMessage"]) == ("ERROR", "call_failed")


async def test_a_generation_with_no_known_model_omits_the_field() -> None:
    spend = Spend(models=(ModelUsage("plan", "", 1, 1, T0, T0),))
    (_, generation) = await _batch(spend)
    assert "model" not in generation["body"]


async def test_a_run_with_no_calls_is_the_trace_alone() -> None:
    events = await _batch(Spend())
    assert [e["type"] for e in events] == ["trace-create"]


async def test_tool_arguments_are_never_exported() -> None:
    """End to end: a loop run whose tool call carries a wallet, through the tracer.

    The asker's own question may name the wallet; the arguments must not
    appear anywhere else in the batch -- not in the span, not in metadata.
    """
    wants = ToolCompletion(
        call=ToolCall(name="issues:search", arguments={"query": "deploys", "wallet": WALLET}),
        model="chat-v1",
    )
    asked = question(text="what did the tracker say")
    outcome = await build_loop(ScriptedSurface(ISSUES, completion=wants)).run(asked)
    sent, client = _capture()
    await LangfuseTracer("http://lf.invalid", "pk", "sk", client=client).trace(
        RunTrace(question=asked, answer=outcome.answer, record=outcome.record)
    )

    events = sent[0]["batch"]
    spans = [e["body"] for e in events if e["type"] == "span-create"]
    assert [s["name"] for s in spans] == ["issues:search"]
    assert all("input" not in s and "arguments" not in s for s in spans)
    wire = json.dumps(sent)
    assert WALLET not in wire
    assert "deploys" not in wire


async def test_an_opted_out_asker_is_still_not_exported_with_usage() -> None:
    inner = CapturingTracer()
    trace = _trace(SPEND)
    opted = FakeOptOut({trace.question.asker.person.platform_user_id})
    await OptOutAwareTracer(inner, opted).trace(trace)
    assert inner.traces == []
    await OptOutAwareTracer(inner, FakeOptOut()).trace(replace(trace))
    assert len(inner.traces) == 1


# --- 2.7 model prices ----------------------------------------------------


class FakeModels:
    """Langfuse's `/api/public/models`, in memory."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": self.rows, "meta": {"totalPages": 1}})
        if request.method == "DELETE":
            model_id = request.url.path.rsplit("/", 1)[-1]
            self.deleted.append(model_id)
            self.rows = [r for r in self.rows if r["id"] != model_id]
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        self.created.append(body)
        self.rows.append({"id": f"new-{len(self.created)}", "isLangfuseManaged": False, **body})
        return httpx.Response(200, json=body)

    def sync(self) -> LangfuseModelSync:
        return LangfuseModelSync(
            "http://lf.invalid", "pk", "sk", transport=httpx.MockTransport(self.handle)
        )


PRICE = ModelPrice("chat-v1", "(?i)^(chat-v1)$", 1e-06, 2e-06)


async def test_a_missing_model_is_created_and_a_second_run_changes_nothing() -> None:
    fake = FakeModels([])
    assert await fake.sync().sync([PRICE]) == {"chat-v1": "created"}
    assert fake.created == [
        {
            "modelName": "chat-v1",
            "matchPattern": "(?i)^(chat-v1)$",
            "unit": "TOKENS",
            "inputPrice": 1e-06,
            "outputPrice": 2e-06,
        }
    ]
    assert await fake.sync().sync([PRICE]) == {"chat-v1": "unchanged"}
    assert len(fake.created) == 1


async def test_a_changed_price_replaces_ours_and_leaves_langfuses_own() -> None:
    fake = FakeModels(
        [
            {"id": "ours", "modelName": "chat-v1", "matchPattern": PRICE.match_pattern,
             "inputPrice": "0.000005", "outputPrice": "0.000002", "isLangfuseManaged": False},
            {"id": "managed", "modelName": "chat-v1", "matchPattern": "x",
             "inputPrice": 1, "outputPrice": 1, "isLangfuseManaged": True},
        ]
    )
    assert await fake.sync().sync([PRICE]) == {"chat-v1": "updated"}
    assert fake.deleted == ["ours"]
    assert len(fake.created) == 1


async def test_a_dry_run_changes_nothing() -> None:
    fake = FakeModels([])
    assert await fake.sync().sync([PRICE], dry_run=True) == {"chat-v1": "created"}
    assert fake.created == []


def test_the_checked_in_table_loads_as_per_token_prices() -> None:
    prices = {p.model_name: p for p in load_price_table(TABLE)}
    assert {"chat-v1", "gpt-4o-mini-transcribe"} <= set(prices)
    transcribe = prices["gpt-4o-mini-transcribe"]
    assert transcribe.input_price == 3.0 / 1_000_000
    # Langfuse prices these itself; a row here would shadow its table.
    assert not {"gpt-4o", "text-embedding-3-small"} & set(prices)
