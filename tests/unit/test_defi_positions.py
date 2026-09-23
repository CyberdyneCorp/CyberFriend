"""Liquidity and lending positions: the arithmetic, the reads, and the guard.

Figures are checked against values worked by hand, and the reads against a
fake chain that answers by function selector -- the same shape as the real
contracts, without the network. The live check against real wallets is in the
PR description, not here: a test that needs Infura is a test that fails when
Infura does.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.aave import AaveReader
from chatmemory.adapters.chain.deployments import DEPLOYMENTS, NATIVE, Deployment
from chatmemory.adapters.chain.liquidity_math import (
    Q128,
    amounts,
    apy_from_ray,
    fees_owed,
    in_range,
    price_at_tick,
    price_from_sqrt,
)
from chatmemory.adapters.chain.node import Call, Node, NodeError
from chatmemory.adapters.chain.positions import (
    ChainLending,
    ChainLiquidity,
    LendingAsset,
    LiquidityPosition,
    TokenDirectory,
    TokenInfo,
)
from chatmemory.adapters.chain.positions_provider import (
    LENDING_TOOL,
    LIQUIDITY_TOOL,
    PositionsProvider,
)
from chatmemory.adapters.chain.positions_render import (
    amount,
    fee_tier,
    render_lending,
    render_liquidity,
)
from chatmemory.adapters.chain.tokens import ARBITRUM, BASE, ETHEREUM
from chatmemory.adapters.chain.uniswap import UniswapReader
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.app.egress import (
    DEFI_POSITIONS_PROVIDER,
    AuthorizedQuery,
    EgressGuard,
    EgressRefused,
    EgressRequest,
    ProvenancedQuery,
    QueryOrigin,
    authorized,
)
from chatmemory.domain.identity import PersonRef

OWNER = "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0"
STRANGER = "0x00000000219ab540356cbb839cbe05303d7705fa"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
ASKER = PersonRef(platform="discord", platform_user_id=7)
Q96 = 2**96


# --- the ABI -----------------------------------------------------------------


def test_every_selector_matches_its_signature() -> None:
    """Selectors are hand-written constants; a typo would call the wrong
    function and decode its answer as if it were the right one."""
    pairs = re.findall(r'^(\w+) = "(0x[0-9a-f]{8})"  # (.+)$', inspect.getsource(abi), re.M)
    assert len(pairs) >= 20
    for name, value, signature in pairs:
        assert abi.selector(signature) == value, name


def test_the_transfer_topic_is_the_erc721_event() -> None:
    expected = "0x" + abi.keccak256(b"Transfer(address,address,uint256)").hex()
    assert expected == abi.TRANSFER_TOPIC


def test_aggregate3_encodes_one_call_as_the_abi_specifies() -> None:
    encoded = abi.aggregate3([(WETH, "0x313ce567")])
    body = encoded.removeprefix(abi.AGGREGATE3)
    words = [body[i : i + 64] for i in range(0, len(body), 64)]
    assert int(words[0], 16) == 0x20  # offset of the array
    assert int(words[1], 16) == 1  # one call
    assert int(words[2], 16) == 0x20  # offset of that call's tuple
    assert words[3] == abi.address(WETH)
    assert int(words[4], 16) == 1  # allowFailure
    assert int(words[5], 16) == 0x60  # offset of calldata within the tuple
    assert int(words[6], 16) == 4  # calldata length
    assert words[7].startswith("313ce567")


def _results(values: list[bytes | None]) -> bytes:
    """What Multicall3 returns: (bool success, bytes data)[]."""
    elements: list[bytes] = []
    for value in values:
        data = value or b""
        padded = data + b"\0" * (-len(data) % 32)
        elements.append(
            (1 if value is not None else 0).to_bytes(32, "big")
            + (64).to_bytes(32, "big")
            + len(data).to_bytes(32, "big")
            + padded
        )
    head = (32).to_bytes(32, "big") + len(values).to_bytes(32, "big")
    offsets, position = b"", 32 * len(values)
    for element in elements:
        offsets += position.to_bytes(32, "big")
        position += len(element)
    return head + offsets + b"".join(elements)


def test_aggregate3_results_decode_with_failures_as_none() -> None:
    values: list[bytes | None] = [(7).to_bytes(32, "big"), None, b"\x01" * 40]
    assert abi.decode_aggregate3(_results(values)) == values


def test_a_string_and_a_bytes32_symbol_both_decode() -> None:
    string = (32).to_bytes(32, "big") + (4).to_bytes(32, "big") + b"USDC".ljust(32, b"\0")
    assert abi.decode_string(string) == "USDC"
    assert abi.decode_string(b"MKR".ljust(32, b"\0")) == "MKR"


def test_int24_ticks_are_sign_extended() -> None:
    assert abi.signed((-196200) % (1 << 24), 24) == -196200
    assert abi.signed(abi.words(bytes.fromhex(abi.uint(-5)))[0], 24) == -5


# --- the node ----------------------------------------------------------------


def _transport(handle: Callable[[dict[str, object]], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: handle(json.loads(r.content)))
    )


async def test_a_rate_limited_call_is_retried_rather_than_failing_the_chain() -> None:
    """Reading three chains at once tripped Infura's per-second limit in
    testing; the chain was then reported unreadable though nothing was wrong."""
    attempts: list[int] = []

    def handle(body: dict[str, object]) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, json={"error": {"code": -32005}})
        seven = "0x" + "00" * 31 + "07"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": seven})

    node = Node("https://x/v3/k", _transport(handle), backoff=(0, 0, 0))
    assert abi.words(await node.eth_call(WETH, "0x313ce567")) == [7]
    assert len(attempts) == 3


async def test_the_key_never_appears_in_a_node_error() -> None:
    def handle(body: dict[str, object]) -> httpx.Response:
        return httpx.Response(401, text="bad key s3cret for https://x/v3/s3cret")

    node = Node("https://x/v3/s3cret", _transport(handle), secret="s3cret", backoff=())
    with pytest.raises(NodeError) as raised:
        await node.eth_call(WETH, "0x313ce567")
    assert "s3cret" not in str(raised.value)


async def test_a_multicall_is_sent_in_chunks() -> None:
    sent: list[int] = []

    def handle(body: dict[str, object]) -> httpx.Response:
        data = body["params"][0]["data"]  # type: ignore[index]
        count = int(data[10 + 64 : 10 + 128], 16)
        sent.append(count)
        result = _results([(i).to_bytes(32, "big") for i in range(count)])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + result.hex()})

    node = Node("https://x", _transport(handle), chunk=2, backoff=())
    results = await node.multicall([Call(WETH, abi.DECIMALS)] * 5)
    assert sent == [2, 2, 1]
    assert len(results) == 5


# --- the arithmetic ----------------------------------------------------------


def test_price_at_tick_zero_is_one_before_decimals() -> None:
    assert price_at_tick(0, 18, 18) == 1
    # WETH (18) / USDC (6): one raw unit ratio of 1 is 10^12 USDC per WETH.
    assert price_at_tick(0, 18, 6) == Decimal(10) ** 12


def test_the_current_price_matches_the_tick_it_came_from() -> None:
    tick = -196_200
    sqrt_x96 = int(Decimal("1.0001") ** (Decimal(tick) / 2) * Q96)
    expected = float(price_at_tick(tick, 18, 6))
    assert float(price_from_sqrt(sqrt_x96, 18, 6)) == pytest.approx(expected, rel=1e-9)


def test_a_range_below_the_price_is_all_token1_and_above_is_all_token0() -> None:
    liquidity = 10**18
    at_one = Q96  # price 1, tick 0
    below = amounts(liquidity, at_one, -200, -100)
    above = amounts(liquidity, at_one, 100, 200)
    inside = amounts(liquidity, at_one, -100, 100)
    assert below[0] == 0 and below[1] > 0
    assert above[0] > 0 and above[1] == 0
    assert inside[0] > 0 and inside[1] > 0
    # Symmetric range around price 1: the two sides hold (almost) the same.
    assert float(inside[0]) == pytest.approx(float(inside[1]), rel=1e-3)


def test_the_upper_tick_is_exclusive_as_in_the_pool() -> None:
    assert in_range(100, 100, 200)
    assert not in_range(200, 100, 200)


def test_fee_growth_that_wrapped_still_yields_the_right_fees() -> None:
    """The counters overflow by design; the difference is taken mod 2^256."""
    last = (1 << 256) - Q128  # one unit of growth before wrapping
    inside = Q128  # one unit after
    assert fees_owed(inside, last, 1000) == 2000


def test_an_aave_rate_becomes_an_apy() -> None:
    five_percent = 5 * 10**25  # 0.05 in ray
    assert apy_from_ray(five_percent) == pytest.approx(Decimal("0.05127"), abs=Decimal("1e-5"))


# --- presenting --------------------------------------------------------------

ETH_TOKEN = TokenInfo(WETH, "WETH", 18)
USDC_TOKEN = TokenInfo(USDC, "USDC", 6)


def _position(**changes: object) -> LiquidityPosition:
    base: dict[str, object] = dict(
        protocol="Uniswap v3", token_id=1, token0=ETH_TOKEN, token1=USDC_TOKEN, fee=500,
        liquidity=10, tick=0, tick_lower=-10, tick_upper=10,
        price=Decimal(2700), price_lower=Decimal(2000), price_upper=Decimal(3000),
        amount0=Decimal(1), amount1=Decimal(2700), fees0=Decimal("0.01"), fees1=Decimal(5),
        usd0=Decimal(2700), usd1=Decimal(1),
    )
    base.update(changes)
    return LiquidityPosition(**base)  # type: ignore[arg-type]


def test_a_position_shows_every_field_that_was_asked_for() -> None:
    text = render_liquidity(OWNER, [ChainLiquidity(BASE, positions=(_position(),))])
    assert "WETH/USDC 0.05%" in text  # pair and fee tier
    assert "in range" in text  # in / out
    assert "Range: 2,000.00 – 3,000.00 USDC per WETH · now 2,700.00" in text
    assert "≈ $5,400.00" in text  # value
    assert "Uncollected: 0.01 WETH + 5 USDC ≈ $32.00" in text
    # Only open positions exist in the result, so nothing about closed ones
    # is said at all.
    assert "closed" not in text


def test_a_stablecoin_first_pair_is_quoted_in_the_stablecoin() -> None:
    """USDC/WETH pools price WETH per USDC; people read USDC per WETH."""
    position = _position(
        token0=USDC_TOKEN, token1=ETH_TOKEN,
        price=Decimal(1) / 2700, price_lower=Decimal(1) / 3000, price_upper=Decimal(1) / 2000,
        amount0=Decimal(2700), amount1=Decimal(1), usd0=Decimal(1), usd1=Decimal(2700),
    )
    text = render_liquidity(OWNER, [ChainLiquidity(BASE, positions=(position,))])
    assert "Range: 2,000.00 – 3,000.00 USDC per WETH · now 2,700.00" in text


def test_out_of_range_and_dynamic_fee_are_said_plainly() -> None:
    text = render_liquidity(
        OWNER, [ChainLiquidity(BASE, positions=(_position(tick=50, fee=0x800000),))]
    )
    assert "out of range" in text
    assert "dynamic fee" in text
    assert fee_tier(3000) == "0.3%"
    assert fee_tier(100) == "0.01%"


def test_a_pool_priced_value_is_flagged() -> None:
    text = render_liquidity(OWNER, [ChainLiquidity(BASE, positions=(_position(pool_priced=True),))])
    assert "valued at this pool's own price" in text


def test_an_unreadable_chain_is_named_and_never_shown_as_empty() -> None:
    text = render_liquidity(
        OWNER, [ChainLiquidity(ETHEREUM), ChainLiquidity(ARBITRUM, unreachable="timed out")]
    )
    assert "**Ethereum** — no open Uniswap positions" in text
    assert "**Arbitrum** — could not be read (timed out)" in text


def _asset(symbol: str, supplied: str, borrowed: str, price: str) -> LendingAsset:
    return LendingAsset(
        token=TokenInfo("0x" + "1" * 40, symbol, 18),
        supplied=Decimal(supplied), borrowed=Decimal(borrowed),
        supply_apy=Decimal("0.0148"), borrow_apy=Decimal("0.05"),
        usd_price=Decimal(price), collateral=True,
    )


def test_aave_lists_supplies_and_borrows_and_hides_dust() -> None:
    chain = ChainLending(
        BASE,
        collateral_usd=Decimal("27914.22"), debt_usd=Decimal("15220.55"),
        health_factor=Decimal("1.438"),
        assets=(
            _asset("WETH", "0.8386", "0", "2721"),
            _asset("USDC", "14733.42", "15220.91", "1"),
            _asset("cbBTC", "0.00000001", "0", "85707"),
        ),
    )
    text = render_lending(OWNER, [chain])
    assert "Collateral $27,914.22 · Debt $15,220.55 · Health factor: 1.44" in text
    supplied, borrowed = text.split("Supplied:")[1].split("Borrowed:")
    assert "WETH" in supplied and "14,733.42 USDC" in supplied
    assert "15,220.91 USDC" in borrowed and "5.00% APY" in borrowed
    assert "cbBTC" not in text
    assert "1 balance(s) under $0.01 not shown" in text


def test_no_debt_is_not_a_health_factor() -> None:
    text = render_lending(OWNER, [ChainLending(BASE, collateral_usd=Decimal(5),
                                               assets=(_asset("WETH", "0.002", "0", "2700"),))])
    assert "Health factor: n/a (no debt)" in text


def test_a_health_factor_near_one_is_flagged() -> None:
    chain = ChainLending(BASE, collateral_usd=Decimal(100), debt_usd=Decimal(95),
                         health_factor=Decimal("1.05"))
    assert "liquidation happens below 1.00" in render_lending(OWNER, [chain])


def test_amounts_read_well_at_any_size() -> None:
    assert amount(Decimal("15220.9112")) == "15,220.91"
    assert amount(Decimal("1.60090000")) == "1.6009"
    assert amount(Decimal("0.000573728")) == "0.000573728"
    assert amount(Decimal("0.00008805")) == "0.00008805"
    assert amount(Decimal("0.00000001")) == "0.00000001"


# --- reading, against a fake chain -------------------------------------------


def _word(*values: int | str) -> bytes:
    out = b""
    for v in values:
        out += (int(v, 16) if isinstance(v, str) else v % (1 << 256)).to_bytes(32, "big")
    return out


class FakeNode:
    """Answers `eth_call` by (target, selector), and multicalls call by call."""

    def __init__(
        self,
        answer: Callable[[str, str], bytes | None],
        transfers_in: dict[int, list[int]] | None = None,
        head: int = 100_000,
    ) -> None:
        self._answer = answer
        #: block -> token IDs transferred to the owner in that block
        self._transfers = transfers_in or {}
        self._head = head
        self.log_ranges: list[tuple[int, int]] = []

    async def block_number(self) -> int:
        return self._head

    async def logs(
        self, address: str, topics: list[str | None], low: int, high: int
    ) -> list[dict[str, object]]:
        assert high - low < 10_000, "Infura refuses wider ranges"
        self.log_ranges.append((low, high))
        return [
            {"topics": [abi.TRANSFER_TOPIC, "0x0", topics[2], hex(token)]}
            for block, tokens in self._transfers.items()
            if low <= block <= high
            for token in tokens
        ]

    def redact(self, text: str) -> str:
        return text

    async def eth_call(self, target: str, data: str, *, sender: str | None = None) -> bytes:
        result = self._answer(target.lower(), data)
        if result is None:
            raise NodeError("reverted")
        return result

    async def multicall(self, calls: list[Call]) -> list[bytes | None]:
        return [self._answer(c.target.lower(), c.data) for c in calls]


BASE_DEPLOYMENT = next(d for d in DEPLOYMENTS if d.chain is BASE)
POOL, DATA, ORACLE = ("0x" + c * 40 for c in "abc")


def _v4_chain(d: Deployment) -> Callable[[str, str], bytes | None]:
    """An owner holding v4 position 1, and position 2 the explorer wrongly
    attributes to them. Native ETH / USDC, price 1 raw (tick 0)."""
    info = (0 << 56) | ((20 % (1 << 24)) << 32) | ((-20 % (1 << 24)) << 8)

    def answer(target: str, data: str) -> bytes | None:
        sel = data[:10]
        arg = data[10:74]
        if sel == abi.BALANCE_OF:
            return _word(1 if target == d.v4_position_manager else 0)
        if sel == abi.OWNER_OF:
            return _word(OWNER if int(arg, 16) == 1 else STRANGER)
        if sel == abi.V4_POOL_AND_POSITION:
            return _word(NATIVE, USDC, 500, 10, 0, info)
        if sel == abi.V4_POSITION_LIQUIDITY:
            return _word(1000)
        if sel == abi.V4_SLOT0:
            return _word(Q96, 0, 0, 500)
        if sel == abi.V4_FEE_GROWTH_INSIDE:
            return _word(2 * Q128, 0)
        if sel == abi.V4_POSITION_INFO:
            return _word(1000, Q128, 0)
        if sel == abi.SYMBOL:
            return (32).to_bytes(32, "big") + (4).to_bytes(32, "big") + b"USDC".ljust(32, b"\0")
        if sel == abi.DECIMALS:
            return _word(6)
        if sel == abi.AAVE_GET_POOL:
            return _word(POOL)
        if sel == abi.AAVE_GET_DATA_PROVIDER:
            return _word(DATA)
        if sel == abi.AAVE_GET_ORACLE:
            return _word(ORACLE)
        if sel == abi.AAVE_ASSET_PRICE:
            return _word(2000 * 10**8 if arg.endswith(d.weth[2:]) else 10**8)
        return None

    return answer


def _explorer(ids: list[int] | None) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if ids is None:
            return httpx.Response(503)
        return httpx.Response(200, json={"items": [{"id": str(i)} for i in ids]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _reader(node: FakeNode, explorer: httpx.AsyncClient) -> UniswapReader:
    tokens = TokenDirectory(node)  # type: ignore[arg-type]
    aave = AaveReader(node, BASE_DEPLOYMENT, tokens)  # type: ignore[arg-type]
    return UniswapReader(node, BASE_DEPLOYMENT, tokens, aave, explorer)  # type: ignore[arg-type]


async def test_a_v4_position_the_chain_does_not_confirm_is_dropped() -> None:
    """The explorer finds IDs; only the chain says whose they are."""
    node = FakeNode(_v4_chain(BASE_DEPLOYMENT))
    result = await _reader(node, _explorer([1, 2])).liquidity(OWNER)

    assert [p.token_id for p in result.positions] == [1]
    position = result.positions[0]
    assert position.protocol == "Uniswap v4"
    assert position.token0.symbol == "ETH"  # native, never a contract call
    assert position.in_range
    # (2 - 1) units of fee growth per unit of liquidity, 1000 liquidity.
    assert position.fees0 == Decimal(1000) / Decimal(10) ** 18
    assert position.usd0 == 2000  # native ETH priced as WETH by the oracle


async def test_a_position_the_explorer_has_not_indexed_is_found_in_recent_blocks() -> None:
    """Regression, from production: a v4 position minted twenty minutes before
    the question was missing from Blockscout on Arbitrum, and the answer said
    "1 Uniswap v4 position(s) could not be listed"."""
    node = FakeNode(_v4_chain(BASE_DEPLOYMENT), transfers_in={85_000: [1]})
    result = await _reader(node, _explorer([])).liquidity(OWNER)

    assert [p.token_id for p in result.positions] == [1]
    assert result.notes == ()
    # Newest first, in windows Infura accepts, stopping once found.
    assert node.log_ranges == [(90_001, 100_000), (80_001, 90_000)]


async def test_a_recent_transfer_the_chain_does_not_confirm_is_dropped() -> None:
    """A position transferred in and out again is in the logs, not the wallet."""
    node = FakeNode(_v4_chain(BASE_DEPLOYMENT), transfers_in={99_000: [2]})
    result = await _reader(node, _explorer(None)).liquidity(OWNER)

    assert result.positions == ()
    assert result.notes == ("1 Uniswap v4 position(s) could not be listed",)
    assert "could not be listed" in render_liquidity(OWNER, [result])


# --- the provider: clearance and isolation -----------------------------------


def _clearance(text: str, question: str, origin: QueryOrigin) -> AuthorizedQuery:
    return EgressGuard().authorize(
        EgressRequest(
            asker=ASKER,
            query=ProvenancedQuery(text=text, origin=origin, question=question),
            provider=DEFI_POSITIONS_PROVIDER,
        )
    )


class Counting:
    def __init__(self) -> None:
        self.requests = 0

    def client(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests += 1
            return httpx.Response(500)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _provider(client: httpx.AsyncClient, budget: int = 3) -> PositionsProvider:
    return PositionsProvider(
        DEPLOYMENTS, "key", CallBudget(budget), RateLimiter(0), client=client, timeout_seconds=2
    )


@pytest.mark.parametrize("origin", [QueryOrigin.ASKER, QueryOrigin.RETRIEVED_CONTENT])
def test_an_address_not_in_the_question_is_refused_clearance(origin: QueryOrigin) -> None:
    """The address a channel message mentions can never become a lookup."""
    with pytest.raises(EgressRefused):
        _clearance(STRANGER, f"pools of {OWNER}", origin)


async def test_the_address_sent_is_the_cleared_one_not_the_models_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arguments are model output; the clearance is the asker's question."""
    looked_up: list[str] = []

    async def report(self: PositionsProvider, tool: str, address: str) -> str:
        looked_up.append(address)
        return "ok"

    monkeypatch.setattr(PositionsProvider, "report", report)
    with authorized(_clearance(OWNER, f"pools of {OWNER}", QueryOrigin.ASKER)):
        await _provider(Counting().client()).call_tool(LIQUIDITY_TOOL, {"address": STRANGER})

    assert looked_up == [OWNER]


async def test_a_rooted_word_that_is_not_an_address_is_refused() -> None:
    counting = Counting()
    provider = _provider(counting.client())
    with authorized(_clearance("pools", "show my pools", QueryOrigin.ASKER)):
        result = await provider.call_tool(LIQUIDITY_TOOL, {"address": "pools"})

    assert result.is_error and "not_an_address" in result.text
    assert counting.requests == 0


async def test_without_a_clearance_nothing_runs() -> None:
    counting = Counting()
    result = await _provider(counting.client()).call_tool(LENDING_TOOL, {"address": OWNER})
    assert result.is_error and "egress_refused" in result.text
    assert counting.requests == 0


async def test_pools_then_loans_about_one_wallet_are_two_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget per (tool, address): asking for loans after pools must not be
    refused as the same question asked twice."""

    async def report(self: PositionsProvider, tool: str, address: str) -> str:
        return f"{tool} for {address}"

    monkeypatch.setattr(PositionsProvider, "report", report)
    provider = _provider(Counting().client(), budget=1)
    question = f"pools and aave of {OWNER}"
    with authorized(_clearance(OWNER, question, QueryOrigin.ASKER)):
        first = await provider.call_tool(LIQUIDITY_TOOL, {"address": OWNER})
        second = await provider.call_tool(LENDING_TOOL, {"address": OWNER})
        third = await provider.call_tool(LIQUIDITY_TOOL, {"address": OWNER})

    assert not first.is_error and not second.is_error
    assert third.is_error and "exhausted" in third.text


async def test_one_chain_failing_does_not_hide_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def liquidity(self: UniswapReader, owner: str) -> ChainLiquidity:
        if self._d.chain is ARBITRUM:
            raise NodeError("HTTP 500 for https://arbitrum-mainnet.infura.io/v3/key")
        return ChainLiquidity(self._d.chain)

    monkeypatch.setattr(UniswapReader, "liquidity", liquidity)
    text = await _provider(Counting().client()).report(LIQUIDITY_TOOL, OWNER)

    assert "**Ethereum** — no open Uniswap positions" in text
    assert "**Base** — no open Uniswap positions" in text
    assert "**Arbitrum** — could not be read" in text
    assert "/v3/key" not in text


def _v3_chain(d: Deployment) -> Callable[[str, str], bytes | None]:
    """Two v3 NFTs: #1 withdrawn with fees left in it, #2 open."""
    v4 = _v4_chain(d)
    pool = "0x" + "d" * 40

    def answer(target: str, data: str) -> bytes | None:
        sel, arg = data[:10], data[10:]
        if sel == abi.BALANCE_OF:
            return _word(2 if target == d.v3_position_manager else 0)
        if sel == abi.TOKEN_OF_OWNER_BY_INDEX:
            return _word(int(arg[64:128], 16) + 1)
        if sel == abi.V3_POSITIONS:
            token = int(arg[:64], 16)
            liquidity, owed = (0, 5 * 10**6) if token == 1 else (1000, 0)
            return _word(0, 0, WETH, USDC, 500, -20, 20, liquidity, 0, 0, owed, owed)
        if sel == abi.V3_GET_POOL:
            return _word(pool)
        if sel == abi.V3_SLOT0:
            return _word(Q96, 0)
        if sel == abi.V3_COLLECT:
            return _word(7, 9)
        return v4(target, data)

    return answer


async def test_only_open_positions_are_read_and_reported() -> None:
    """A withdrawn NFT is left out even with fees in it: the report is of the
    positions that are earning, not the history of every one ever opened."""
    node = FakeNode(_v3_chain(BASE_DEPLOYMENT))
    result = await _reader(node, _explorer([])).liquidity(OWNER)

    assert [(p.protocol, p.token_id) for p in result.positions] == [("Uniswap v3", 2)]
    text = render_liquidity(OWNER, [result])
    assert "#1" not in text and "closed" not in text
