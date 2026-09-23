"""Creation's one read: open positions pinned for the sweep, Aave accounts, and failures.

Over the same scripted node the watcher's tests use (`FakeChain`), answering
the positions reader's discovery calls too, so what is pinned here is what the
sweep will later read: the v3 pool address, the symbols, decimals and fee, and
a baseline computed by the sweep's own function.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from chatmemory.adapters.chain.alert_targets import ChainTargets
from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.web.limits import RateLimiter
from chatmemory.ports.alerts import AlertKind, AlertState, LpProtocol, LpTarget
from tests.e2e.harness.chain import AaveAccount, FakeChain, V3Position, V4Position
from tests.e2e.harness.web import INFURA_HOSTS, FakeWeb

WALLET = "0xdd8a0000000000000000000000000000000063d6"
OTHER = "0x0000000000000000000000000000000000000bad"
POOL = "0xd0b53d9277642d899df5c87a3966a349a798f224"


def nodes() -> dict[str, FakeChain]:
    chains = {host: FakeChain(d) for host, d in zip(INFURA_HOSTS, DEPLOYMENTS, strict=True)}
    base = chains["base-mainnet.infura.io"]
    base.v3[4558452] = V3Position(WALLET, POOL, -199560, -195770, 10**15)
    base.v3[4558505] = V3Position(WALLET, POOL, -199560, -197500, 10**15)
    base.v3[9] = V3Position(OTHER, POOL, -199560, -195770, 10**15)
    base.v3[10] = V3Position(WALLET, POOL, -199560, -195770, 0)
    base.ticks[POOL] = -197404
    base.aave[WALLET] = AaveAccount(Decimal("27699.05"), Decimal("15221.12"), Decimal("1.43"))
    return chains


def targets(chains: dict[str, FakeChain]) -> tuple[ChainTargets, FakeWeb]:
    web = FakeWeb({host: node.handle for host, node in chains.items()})
    return ChainTargets("key", transport=web.transport, limiter=RateLimiter(0)), web


async def test_open_positions_are_pinned_with_what_the_sweep_needs() -> None:
    reader, _ = targets(nodes())

    read = await reader.read(WALLET, AlertKind.LP_RANGE, None)

    assert read.unreachable == () and read.health == ()
    by_id = {c.target.token_id: c for c in read.lp}
    assert set(by_id) == {4558452, 4558505}, "someone else's and a withdrawn one are not offered"
    first = by_id[4558452]
    assert first.chain == "base"
    assert first.target == LpTarget(
        LpProtocol.UNISWAP_V3, 4558452, POOL, "WETH", "USDC", 18, 6, 500
    )
    assert first.state is AlertState.IN_RANGE and first.tick == -197404
    assert (first.base_symbol, first.quote_symbol) == ("WETH", "USDC")
    assert first.price_lower < first.price < first.price_upper
    assert by_id[4558505].state is AlertState.OUT_OF_RANGE


async def test_an_aave_account_is_read_per_chain() -> None:
    reader, _ = targets(nodes())

    read = await reader.read(WALLET, AlertKind.AAVE_HEALTH, None)

    by_chain = {c.chain: c.health_factor for c in read.health}
    assert by_chain == {"ethereum": None, "base": Decimal("1.43"), "arbitrum": None}


async def test_a_named_chain_is_the_only_one_read() -> None:
    reader, web = targets(nodes())

    read = await reader.read(WALLET, AlertKind.AAVE_HEALTH, "base")

    assert [c.chain for c in read.health] == ["base"]
    assert web.hosts() == {"base-mainnet.infura.io"}


async def test_a_chain_that_fails_is_named_and_the_others_still_read() -> None:
    chains = nodes()
    chains["arbitrum-mainnet.infura.io"].failing = True
    reader, _ = targets(chains)

    read = await reader.read(WALLET, AlertKind.AAVE_HEALTH, None)

    assert read.unreachable == ("arbitrum",)
    assert {c.chain for c in read.health} == {"ethereum", "base"}


async def test_chains_that_cannot_be_reached_are_unreachable_not_empty() -> None:
    """"Could not see it" must never read as "has no positions"."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    web = FakeWeb(dict.fromkeys(INFURA_HOSTS, refuse))
    reader = ChainTargets("sekret", transport=web.transport, limiter=RateLimiter(0))

    read = await reader.read(WALLET, AlertKind.LP_RANGE, None)

    assert read.unreachable == ("ethereum", "base", "arbitrum")
    assert read.lp == ()
    assert "sekret" not in repr(read), "the endpoint URL carries the key"


async def test_a_v4_position_is_pinned_by_its_pool_id() -> None:
    """The v4 manager lists nothing, so the id comes from the explorer and the
    pool id -- what the sweep reads the tick from -- from the position's key."""
    usdc = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
    chains = nodes()
    arbitrum = chains["arbitrum-mainnet.infura.io"]
    position = V4Position(WALLET, (0, int(usdc, 16), 500, 10, 0), -197920, -197070, 10**15)
    arbitrum.v4[210171] = position
    arbitrum.tokens[usdc] = ("USDC", 6)
    arbitrum.ticks[position.pool_id] = -197404
    reader, web = targets(chains)

    def explorer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "210171"}], "next_page_params": None})

    web.script("arbitrum.blockscout.com", explorer)

    read = await reader.read(WALLET, AlertKind.LP_RANGE, "arbitrum")

    [found] = read.lp
    assert found.target == LpTarget(
        LpProtocol.UNISWAP_V4, 210171, position.pool_id, "ETH", "USDC", 18, 6, 500
    )
    assert found.state is AlertState.IN_RANGE
