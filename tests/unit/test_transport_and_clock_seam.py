"""The HTTP transport and the clock, as edges.

`Edges.http_transport` is what every HTTP client the federation's local
providers, the attachment fetchers and the tracer open sends through, and
`Edges.clock` is what the time route, the prompt's clock notice and the bot's
loops read as "now". An end-to-end test replaces both and nothing else, so
each is proved to reach the code that uses it -- and an HTTP client opened
without the transport is caught before it quietly reaches the network from a
test that believed it was sealed.
"""

from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.chain.provider import WALLET_TOOL, WalletProvider
from chatmemory.app.authorization import ActionOrigin, InvocationRequest
from chatmemory.composition import build_answer_stack, build_federation
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import Viewer
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


async def test_a_wallet_lookup_goes_through_the_edges_transport(
    engine: AsyncEngine,  # noqa: F811 - the fixture
) -> None:
    """The balance batch reaches the transport the edges hold, not the network."""
    batches: list[list[dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            # The USD price lookup rides the same transport; no price is fine.
            return httpx.Response(200, json={})
        batch = json.loads(request.content)
        batches.append(batch)
        return httpx.Response(
            200, json=[{"jsonrpc": "2.0", "id": c["id"], "result": "0x0"} for c in batch]
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
    assert all(c["params"][0] == ADDRESS.lower() for c in balances)  # type: ignore[index]


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
