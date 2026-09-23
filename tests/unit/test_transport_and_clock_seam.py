"""The HTTP transport and the clock, as edges.

`Edges.http_transport` is what every HTTP client the federation's local
providers and the tracer open sends through, and `Edges.clock` is what the
time route, the prompt's clock notice, catch-up and the bot's loops read as
"now". An end-to-end test replaces both and nothing else, so each is proved
to reach the code that uses it by a request arriving at a mock or a fixed
time appearing in a prompt -- and an HTTP client opened without the transport
keyword is caught before it quietly reaches the network from a test that
believed it was sealed. The document fetchers and the trace deleter take the
keyword too, but no process built over the edges constructs them yet.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory import composition
from chatmemory.adapters.chain.provider import WALLET_TOOL, WalletProvider
from chatmemory.adapters.web.query import web_arguments
from chatmemory.app.authorization import ActionOrigin, InvocationRequest
from chatmemory.app.catchup import catch_up_request
from chatmemory.app.memory import Recollection
from chatmemory.app.reasoning.service import build_answer_service
from chatmemory.app.reasoning.stages import clock_notice
from chatmemory.composition import (
    build_answer_stack,
    build_catch_up,
    build_federation,
    build_tracer,
)
from chatmemory.config import Settings
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery
from chatmemory.entrypoints.bot import notification_loop, scheduled_task_loop
from chatmemory.health import HealthState
from chatmemory.ports.answers import Question
from tests.unit.test_assembly_seam import (
    BOT,
    COMPOSITION,
    SRC,
    _calls,
    _function,
    engine,  # noqa: F401 - the fixture, used by name
    fake_edges,
    settings,
)
from tests.unit.test_catchup import FakeChat as RecordingChat
from tests.unit.test_memory_integration import (
    ConversationalModel,
    TopicRetrieval,
    ask,
    turn,
)
from tests.unit.test_run_tracing import FakeIndex, FakeOptOut, _trace
from tests.unit.test_tracing_wiring import CONFIGURED
from tests.unit.test_wallet_balances import ADDRESS, ASKER

GUARDED = ("chain", "market", "web", "documents", "tracing")
FIXED = datetime(2031, 3, 4, 5, 6, tzinfo=UTC)


# --- every client opened takes the transport ----------------------------


def _clients_without_transport(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    return [
        f"{path.relative_to(SRC)}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "AsyncClient"
        and not any(k.arg == "transport" for k in node.keywords)
    ]


def test_every_http_client_the_adapters_open_takes_the_transport() -> None:
    """One client opened without it is one request an end-to-end test sends
    to the real network while believing it is sealed."""
    files = [p for d in GUARDED for p in sorted((SRC / "adapters" / d).rglob("*.py"))]
    assert files, "the guarded adapter packages moved; update GUARDED"
    missing = [where for path in files for where in _clients_without_transport(path)]
    assert not missing, f"httpx.AsyncClient opened without transport= at {missing}"


def test_the_answer_stack_hands_the_transport_to_federation_and_tracing() -> None:
    stack = _function(COMPOSITION, "build_answer_stack")
    for callee in ("build_federation", "build_tracer"):
        [call] = _calls(stack, callee)
        passed = [*call.args, *(k.value for k in call.keywords)]
        assert any(ast.unparse(a) == "edges.http_transport" for a in passed), (
            f"build_answer_stack must pass edges.http_transport to {callee}"
        )


def _balance(method: str) -> str:
    return hex(10**18) if method == "eth_getBalance" else "0x0"


async def test_a_wallet_lookup_goes_through_the_edges_transport(
    engine: AsyncEngine,  # noqa: F811 - the fixture
) -> None:
    """The balance batch and the USD price lookup both reach the transport
    the edges hold, not the network."""
    batches: list[list[dict[str, Any]]] = []
    price_lookups: list[httpx.URL] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            # The USD price lookup rides the same transport; no price is fine.
            price_lookups.append(request.url)
            return httpx.Response(200, json={})
        batch = json.loads(request.content)
        batches.append(batch)
        # One ether on every chain, so the balance is worth pricing in USD.
        return httpx.Response(
            200,
            json=[
                {"jsonrpc": "2.0", "id": c["id"], "result": _balance(c["method"])} for c in batch
            ],
        )

    edges = replace(fake_edges(engine), http_transport=httpx.MockTransport(handle))
    tools = await build_federation(
        settings(wallet_tools_enabled=True, infura_key="key"),
        transport=edges.http_transport,
    )
    assert tools is not None
    try:
        outcome = await tools.surface.invoke(
            InvocationRequest(
                requester=ASKER,
                question=f"what does {ADDRESS} hold?",
                qualified_name=f"{WalletProvider.server}:{WALLET_TOOL}",
                arguments={"address": ADDRESS},
                origin=ActionOrigin.REQUESTER_REQUEST,
            )
        )
    finally:
        await tools.federation.aclose()

    assert outcome.invoked, outcome.detail
    assert batches, "the balance batch never reached the edges' transport"
    balances = [c for batch in batches for c in batch if c["method"] == "eth_getBalance"]
    assert balances, "no eth_getBalance was sent"
    assert all(c["params"][0] == ADDRESS.lower() for c in balances)
    assert price_lookups and {u.host for u in price_lookups} == {"api.coingecko.com"}, (
        "the wallet's USD price lookup never reached the edges' transport"
    )


# One call per local provider the federation registers besides the wallet:
# what it is enabled by, the tool, its arguments and the host it must reach.
PROVIDER_CALLS = [
    pytest.param(
        {"web_tools_enabled": True},
        "wikipedia:search",
        web_arguments("who was Frank Herbert", "Frank Herbert"),
        "en.wikipedia.org",
        id="web-wikipedia",
    ),
    pytest.param(
        {"web_tools_enabled": True, "serpapi_key": "key"},
        "serpapi:search",
        web_arguments("who was Frank Herbert", "Frank Herbert"),
        "serpapi.com",
        id="web-serpapi",
    ),
    pytest.param(
        {"market_tools_enabled": True},
        "market_crypto:crypto_price",
        {"asset": "ETH"},
        "api.coingecko.com",
        id="market-coingecko",
    ),
    pytest.param(
        {"market_tools_enabled": True},
        "market_fx:convert",
        {"amount": 100, "from": "USD", "to": "BRL"},
        "api.frankfurter.dev",
        id="market-frankfurter",
    ),
    pytest.param(
        {"market_tools_enabled": True, "serpapi_key": "key"},
        "market_index:index_level",
        {"index": "SPX"},
        "serpapi.com",
        id="market-google-finance",
    ),
]


@pytest.mark.parametrize(("enabled", "tool", "arguments", "host"), PROVIDER_CALLS)
async def test_every_local_provider_sends_through_the_edges_transport(
    enabled: dict[str, object], tool: str, arguments: Mapping[str, object], host: str
) -> None:
    """The keyword being present is not the value arriving: each provider is
    invoked through `build_federation` and must reach the mock, not the net."""
    seen: list[httpx.URL] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={})

    tools = await build_federation(settings(**enabled), transport=httpx.MockTransport(handle))
    assert tools is not None
    try:
        await tools.surface.invoke(
            InvocationRequest(
                requester=ASKER,
                question="who was Frank Herbert, convert 100 USD to BRL, "
                "what is ETH and the S&P 500 (SPX) at?",
                qualified_name=tool,
                arguments=dict(arguments),
                origin=ActionOrigin.REQUESTER_REQUEST,
            )
        )
    finally:
        await tools.federation.aclose()

    assert host in {u.host for u in seen}, f"{tool} never reached the edges' transport"


async def test_the_tracer_exports_through_the_transport_it_is_handed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_tracer` must hand its transport to the Langfuse exporter."""
    posts: list[httpx.URL] = []

    def handle(request: httpx.Request) -> httpx.Response:
        posts.append(request.url)
        return httpx.Response(207, json={"successes": [], "errors": []})

    # The index and the opt-out registry are Postgres; neither is under test.
    monkeypatch.setattr(composition, "PostgresTraceIndex", lambda _: FakeIndex())
    monkeypatch.setattr(composition, "PostgresRetentionStore", lambda _: FakeOptOut())
    tracer = build_tracer(
        Settings(**CONFIGURED),
        object(),
        httpx.MockTransport(handle),  # type: ignore[arg-type]
    )
    assert tracer is not None

    await tracer.trace(_trace())

    assert [u.path for u in posts] == ["/api/public/ingestion"], (
        "the trace export never reached the transport build_tracer was handed"
    )


# --- the clock ----------------------------------------------------------


def _question(text: str) -> Question:
    return Question(
        text=text,
        asker=Viewer(person=ASKER, visible_channels=frozenset()),
        audience=Audience(DeliveryMode.DIRECT_MESSAGE, frozenset({ASKER}), frozenset()),
    )


async def test_a_fixed_clock_in_the_edges_is_the_time_the_route_answers(
    engine: AsyncEngine,  # noqa: F811 - the fixture
) -> None:
    edges = replace(fake_edges(engine), clock=lambda: FIXED)
    stack = await build_answer_stack(settings(), edges=edges)

    answer = await stack.answers.answer(_question("what's today's date ?"))

    assert "04 March 2031, 05:06 UTC" in answer.text


def test_the_process_hands_the_edges_clock_to_catch_up() -> None:
    [call] = _calls(_function(BOT, "assemble"), "build_catch_up")
    passed = [*call.args, *(k.value for k in call.keywords)]
    assert any(ast.unparse(a) == "edges.clock" for a in passed), (
        "assemble must build catch-up on the clock the process was assembled over"
    )


async def test_the_planner_and_synthesiser_state_the_clock_they_are_given() -> None:
    model, retrieval = ConversationalModel(), TopicRetrieval()
    answers = build_answer_service(retrieval, model, clock=lambda: FIXED)  # type: ignore[arg-type]

    await answers.answer_run(ask("and last month?", Recollection(turns=(turn(),))))

    notice = clock_notice(FIXED)
    for stage in ("question_plan", "grounded_answer"):
        [(system, _)] = model.prompts[stage]
        assert notice in system, f"the {stage} prompt did not state the given clock"


GENERAL = ChannelRef("discord", 100)


class _Search:
    """One window in #general, and every query it was asked."""

    def __init__(self) -> None:
        self.queries: list[SearchQuery] = []

    async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
        self.queries.append(query)
        return [
            SearchHit(
                window_id=42,
                channel=GENERAL,
                text="we shipped the new pricing page",
                starts_at=FIXED,
                ends_at=FIXED,
                score=0.9,
                relevance_source=RelevanceSource.FUSED_RRF,
            )
        ]


async def test_catch_up_reads_the_clock_it_is_built_on() -> None:
    """Where "since" ends and what the prompt says the time is, both."""
    search, chat = _Search(), RecordingChat()
    catchup = build_catch_up(settings(), search, chat, lambda: FIXED)  # type: ignore[arg-type]
    request = catch_up_request("what did I miss in <#100> today")
    assert request is not None

    await catchup.summarise(
        Question(
            text="what did I miss in <#100> today",
            asker=Viewer(person=ASKER, visible_channels=frozenset({GENERAL})),
            audience=Audience(
                DeliveryMode.DIRECT_MESSAGE, frozenset({ASKER}), frozenset({GENERAL})
            ),
        ),
        request,
        None,
    )

    [query] = search.queries
    assert query.since == FIXED.replace(hour=0, minute=0), '"today" was not the given clock\'s day'
    [(system, _)] = chat.prompts
    assert clock_notice(FIXED) in system


class _Runner:
    def __init__(self) -> None:
        self.at: list[datetime] = []

    async def run_due(self, now: datetime) -> int:
        self.at.append(now)
        raise asyncio.CancelledError


class _Delivery(_Runner):
    async def deliver(self, now: datetime) -> object:
        return await self.run_due(now)


async def test_the_loops_read_the_clock_they_are_given() -> None:
    runner, delivery = _Runner(), _Delivery()
    with pytest.raises(asyncio.CancelledError):
        await scheduled_task_loop(runner, HealthState(), clock=lambda: FIXED)  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await notification_loop(delivery, HealthState(), clock=lambda: FIXED)  # type: ignore[arg-type]
    assert runner.at == [FIXED]
    assert delivery.at == [FIXED]


def test_main_starts_both_loops_on_the_edges_clock() -> None:
    main = _function(BOT, "main")
    for loop in ("scheduled_task_loop", "notification_loop"):
        [call] = _calls(main, loop)
        clock = next((k.value for k in call.keywords if k.arg == "clock"), None)
        assert clock is not None and ast.unparse(clock) == "process.edges.clock", (
            f"main must start {loop} on the clock the process was assembled over"
        )
