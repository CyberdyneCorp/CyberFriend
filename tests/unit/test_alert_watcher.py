"""The chain watcher: one multicall per chain, decoded into readings.

Driven over a `MockTransport` answering as a node holding the positions from
the design's live reads (#4558452 on Base, #210171 on Arbitrum, an Aave loan at
HF 1.4268), so the calldata and the decoding are the real ones end to end.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.chain.node import MULTICALL3
from chatmemory.adapters.chain.watch import ChainWatcher
from chatmemory.adapters.web.limits import RateLimiter
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.alerts import (
    AlertKind,
    AlertLanguage,
    AlertState,
    HealthObservation,
    LpObservation,
    LpProtocol,
    LpTarget,
    PositionAlert,
    ReadFailure,
)
from tests.e2e.harness.chain import (
    AaveAccount,
    FakeChain,
    V3Position,
    V4Position,
    decode_aggregate3_call,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
BASE, ARBITRUM = DEPLOYMENTS[1], DEPLOYMENTS[2]
LEO = PersonRef("discord", 7)
WALLET = "0xdd8a0000000000000000000000000000000063d6"
OTHER = "0x0000000000000000000000000000000000000bad"
POOL = "0xd0b53d9277642d899df5c87a3966a349a798f224"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
V4_KEY = (0, int("af88d065e77c8cc2239327c5edb3a432268e5831", 16), 500, 10, 0)


def alert(alert_id: int, *, chain: str = "base", lp: LpTarget | None = None) -> PositionAlert:
    return PositionAlert(
        id=alert_id,
        person=LEO,
        kind=AlertKind.LP_RANGE if lp else AlertKind.AAVE_HEALTH,
        chain=chain,
        address=WALLET,
        language=AlertLanguage.ENGLISH,
        state=AlertState.IN_RANGE if lp else AlertState.OK,
        state_since=NOW,
        created_at=NOW,
        next_check_at=NOW,
        lp=lp,
        threshold=None if lp else Decimal("1.3"),
    )


V3 = LpTarget(LpProtocol.UNISWAP_V3, 4558452, POOL, "WETH", "USDC", 18, 6, 500)
V4 = LpTarget(
    LpProtocol.UNISWAP_V4,
    210171,
    V4Position(WALLET, V4_KEY, 0, 0, 0).pool_id,
    "ETH",
    "USDC",
    18,
    6,
    500,
)


def base_chain() -> FakeChain:
    chain = FakeChain(BASE)
    chain.v3[4558452] = V3Position(WALLET, POOL, -199560, -195770, 10**15, WETH, USDC)
    chain.ticks[POOL] = -197404
    chain.aave[WALLET] = AaveAccount(
        Decimal("27699.05"), Decimal("15221.12"), Decimal("1.426788938")
    )
    return chain


def arbitrum_chain() -> FakeChain:
    chain = FakeChain(ARBITRUM)
    position = V4Position(WALLET, V4_KEY, -197920, -197070, 10**14)
    chain.v4[210171] = position
    chain.ticks[position.pool_id] = -197404
    return chain


def watcher(*chains: FakeChain, **kwargs: object) -> tuple[ChainWatcher, list[httpx.Request]]:
    by_host = {c.deployment.chain.infura_host + ".infura.io": c for c in chains}
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return by_host[request.url.host].handle(request)

    built = ChainWatcher(
        "key",
        limiter=RateLimiter(0),
        transport=httpx.MockTransport(handle),
        **kwargs,  # type: ignore[arg-type]
    )
    return built, seen


async def test_a_pinned_v3_position_in_range() -> None:
    chain = base_chain()
    reader, _ = watcher(chain)

    [reading] = (await reader.observe([alert(1, lp=V3)])).values()

    assert isinstance(reading, LpObservation)
    assert reading.state is AlertState.IN_RANGE
    assert (reading.tick, reading.tick_lower, reading.tick_upper) == (-197404, -199560, -195770)
    assert (reading.base_symbol, reading.quote_symbol) == ("WETH", "USDC")
    assert Decimal("2674") < reading.price < Decimal("2676")
    assert Decimal("2156") < reading.price_lower < Decimal("2157")
    assert Decimal("3149") < reading.price_upper < Decimal("3150")


async def test_out_of_range_when_the_pool_moves() -> None:
    chain = base_chain()
    chain.ticks[POOL] = -195000
    reader, _ = watcher(chain)

    [reading] = (await reader.observe([alert(1, lp=V3)])).values()

    assert isinstance(reading, LpObservation) and reading.state is AlertState.OUT_OF_RANGE


@pytest.mark.parametrize("how", ["burned", "transferred", "withdrawn"])
async def test_a_closed_v3_position(how: str) -> None:
    chain = base_chain()
    if how == "burned":
        del chain.v3[4558452]
    elif how == "transferred":
        chain.v3[4558452].owner = OTHER
    else:
        chain.v3[4558452].liquidity = 0
    reader, _ = watcher(chain)

    [reading] = (await reader.observe([alert(1, lp=V3)])).values()

    assert isinstance(reading, LpObservation) and reading.state is AlertState.CLOSED


async def test_a_pinned_v4_position_reads_the_stored_pool_id() -> None:
    chain = arbitrum_chain()
    reader, seen = watcher(chain)

    [reading] = (await reader.observe([alert(2, chain="arbitrum", lp=V4)])).values()

    assert isinstance(reading, LpObservation)
    assert reading.state is AlertState.IN_RANGE
    assert (reading.tick_lower, reading.tick_upper) == (-197920, -197070)
    assert Decimal("2540") < reading.price_lower < Decimal("2541")
    [request] = seen
    calls = decode_aggregate3_call(json.loads(request.content)["params"][0]["data"])
    assert calls[-1] == (
        ARBITRUM.v4_state_view,
        abi.V4_SLOT0 + V4.pool_ref.removeprefix("0x"),
    )


async def test_a_v4_position_with_no_liquidity_is_closed() -> None:
    chain = arbitrum_chain()
    chain.v4[210171].liquidity = 0
    reader, _ = watcher(chain)

    [reading] = (await reader.observe([alert(2, chain="arbitrum", lp=V4)])).values()

    assert isinstance(reading, LpObservation) and reading.state is AlertState.CLOSED


async def test_the_aave_health_factor() -> None:
    reader, _ = watcher(base_chain())

    [reading] = (await reader.observe([alert(3)])).values()

    assert isinstance(reading, HealthObservation)
    assert reading.health_factor == Decimal("1.426788938")
    assert reading.collateral_usd == Decimal("27699.05")
    assert reading.debt_usd == Decimal("15221.12")


async def test_no_debt_is_no_health_factor() -> None:
    chain = base_chain()
    chain.aave[WALLET] = AaveAccount(Decimal("5.21"), Decimal(0), None)
    reader, _ = watcher(chain)

    [reading] = (await reader.observe([alert(3)])).values()

    assert isinstance(reading, HealthObservation) and reading.health_factor is None


async def test_every_alert_on_a_chain_is_one_multicall() -> None:
    """The cost the design rests on: a sweep is about one request per chain."""
    base, arbitrum = base_chain(), arbitrum_chain()
    reader, seen = watcher(base, arbitrum)
    alerts = [alert(1, lp=V3), alert(3), alert(2, chain="arbitrum", lp=V4)]

    readings = await reader.observe(alerts)
    # The Aave pool is resolved once and kept: the second sweep is one call.
    await reader.observe(alerts)

    assert set(readings) == {1, 2, 3}
    multicalls = [
        r for r in seen if json.loads(r.content)["params"][0]["to"] == MULTICALL3
    ]
    assert len(multicalls) == 4, "one per chain per sweep"
    assert len(seen) == 5, "plus the Aave pool, once"


async def test_a_chain_that_fails_is_a_failure_for_each_of_its_alerts_only() -> None:
    base, arbitrum = base_chain(), arbitrum_chain()
    base.failing = True
    reader, _ = watcher(base, arbitrum)

    readings = await reader.observe(
        [alert(1, lp=V3), alert(3), alert(2, chain="arbitrum", lp=V4)]
    )

    assert isinstance(readings[1], ReadFailure)
    assert isinstance(readings[3], ReadFailure)
    assert isinstance(readings[2], LpObservation)


async def test_a_pool_that_does_not_answer_is_a_failure_not_a_closed_position() -> None:
    chain = base_chain()
    del chain.ticks[POOL]
    reader, _ = watcher(chain)

    [reading] = (await reader.observe([alert(1, lp=V3)])).values()

    assert isinstance(reading, ReadFailure)


async def test_alerts_past_the_per_chain_cap_wait_for_the_next_sweep() -> None:
    reader, _ = watcher(base_chain(), max_calls_per_chain=4)

    readings = await reader.observe([alert(1, lp=V3), alert(3), replace(alert(4), id=4)])

    assert isinstance(readings[1], LpObservation)
    assert isinstance(readings[3], HealthObservation)
    assert isinstance(readings[4], ReadFailure)


async def test_the_key_never_reaches_a_log_line(capsys: pytest.CaptureFixture[str]) -> None:
    """The endpoint URL carries the key, and a connection error quotes the URL."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    reader = ChainWatcher(
        "secret-key", limiter=RateLimiter(0), transport=httpx.MockTransport(unreachable)
    )

    [reading] = (await reader.observe([alert(3)])).values()

    assert isinstance(reading, ReadFailure)
    logged = capsys.readouterr().out
    assert "alerts.chain_failed" in logged
    assert "secret-key" not in logged
