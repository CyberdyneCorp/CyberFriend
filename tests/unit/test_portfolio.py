"""Portfolio totals: the arithmetic, the reads, the clearance and the answer.

The totals are checked on values built by hand; the reads against the harness's
fake chain, which answers by function selector the way the contracts do. The
figures are reduced from a live read of two real wallets, which is also where
each pitfall below was found:

*   an aToken or debt token read as a wallet balance counts an Aave supply
    twice and a debt as an asset;
*   collateral minus debt leaves out a supply that is not collateral;
*   a chain that could not be read quietly lowers a plain total;
*   the balances path had no back-off, and Arbitrum came back "could not be
    reached" straight after a positions read that a retry would have answered.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.aave import ReserveCache
from chatmemory.adapters.chain.clearance import MAX_ADDRESSES, Cleared, clear_addresses
from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.chain.node import Node, rate_limited
from chatmemory.adapters.chain.portfolio import (
    ChainPortfolio,
    ChainWallet,
    Held,
    Section,
    WalletPortfolio,
    total,
)
from chatmemory.adapters.chain.portfolio_render import portfolio_language, render_portfolio
from chatmemory.adapters.chain.positions import (
    ChainLending,
    ChainLiquidity,
    LendingAsset,
    LiquidityPosition,
    TokenInfo,
)
from chatmemory.adapters.chain.positions_provider import PORTFOLIO_TOOL, PositionsProvider
from chatmemory.adapters.chain.provider import PricedAsset
from chatmemory.adapters.chain.rpc import ChainReader
from chatmemory.adapters.chain.tokens import ARBITRUM, BASE, ETHEREUM, Chain, tokens_for
from chatmemory.adapters.mcp_client.session import ToolResult
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
from chatmemory.app.language import Language
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.facts import MAX_WALLETS_PER_KIND
from tests.e2e.harness.chain import (
    AAVE_POOL,
    CBBTC,
    USDC,
    WETH,
    AaveReserve,
    FakeChain,
    decode_aggregate3_call,
)
from tests.e2e.harness.web import json_rpc

WALLET = "0xdd8ac30cb0a219af4963eab5e30ed065b5e63d68"
SECOND = "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0"
ASKER = PersonRef(platform="discord", platform_user_id=7)

ETH_USD = Decimal("2000")
BTC_USD = Decimal("80000")

WETH_INFO = TokenInfo(WETH, "WETH", 18)
USDC_INFO = TokenInfo(USDC, "USDC", 6)
CBBTC_INFO = TokenInfo(CBBTC, "cbBTC", 8)


# --- the arithmetic -------------------------------------------------------------


def _asset(
    token: TokenInfo,
    supplied: str = "0",
    borrowed: str = "0",
    price: Decimal | None = None,
    collateral: bool = True,
) -> LendingAsset:
    return LendingAsset(
        token=token,
        supplied=Decimal(supplied),
        borrowed=Decimal(borrowed),
        supply_apy=Decimal(0),
        borrow_apy=Decimal(0),
        usd_price=price,
        collateral=collateral,
    )


def _position(fees0: str = "0", usd: Decimal | None = ETH_USD) -> LiquidityPosition:
    return LiquidityPosition(
        protocol="Uniswap v3",
        token_id=4558452,
        token0=WETH_INFO,
        token1=USDC_INFO,
        fee=500,
        liquidity=1,
        tick=0,
        tick_lower=-10,
        tick_upper=10,
        price=ETH_USD,
        price_lower=Decimal(1),
        price_upper=Decimal(3000),
        amount0=Decimal(1),
        amount1=Decimal(1000),
        fees0=Decimal(fees0),
        fees1=Decimal(0),
        usd0=usd,
        usd1=Decimal(1),
    )


def _chain(
    holdings: tuple[Held, ...] = (),
    positions: tuple[LiquidityPosition, ...] = (),
    assets: tuple[LendingAsset, ...] = (),
    *,
    chain: Chain = BASE,
    unread: Section | None = None,
) -> ChainPortfolio:
    def reason(section: Section) -> str:
        return "timed out" if unread is section else ""

    return ChainPortfolio(
        wallet=ChainWallet(chain, holdings, unreachable=reason(Section.WALLET)),
        liquidity=ChainLiquidity(chain, positions, unreachable=reason(Section.LIQUIDITY)),
        lending=ChainLending(chain, assets=assets, unreachable=reason(Section.LENDING)),
    )


def test_the_total_is_wallet_plus_positions_with_fees_plus_the_aave_net() -> None:
    chain = _chain(
        holdings=(Held("ETH", Decimal("0.5"), ETH_USD), Held("USDC", Decimal(100), Decimal(1))),
        positions=(_position(fees0="0.01"),),
        assets=(_asset(WETH_INFO, supplied="1", price=ETH_USD),),
    )

    grand = total([WalletPortfolio(WALLET, (chain,))])

    # 1,000 + 100 in the wallet; 2,000 + 1,000 in the pool and 20 of fees;
    # 2,000 on Aave.
    assert grand.usd == Decimal(6120)
    assert grand.complete
    assert chain.fees_usd() == Decimal(20)


def test_a_debt_is_subtracted_and_never_counted_as_an_asset() -> None:
    chain = _chain(
        assets=(
            _asset(WETH_INFO, supplied="1", price=ETH_USD),
            _asset(USDC_INFO, borrowed="1500", price=Decimal(1)),
        )
    )
    assert chain.lending_net_usd() == Decimal(500)


def test_the_aave_net_counts_a_supply_that_is_not_collateral() -> None:
    """The account's collateral minus debt would report 2,000 - 1,500 here and
    leave the 80,000 of cbBTC out."""
    lending = ChainLending(
        BASE,
        collateral_usd=Decimal(2000),
        debt_usd=Decimal(1500),
        assets=(
            _asset(WETH_INFO, supplied="1", price=ETH_USD),
            _asset(USDC_INFO, borrowed="1500", price=Decimal(1)),
            _asset(CBBTC_INFO, supplied="1", price=BTC_USD, collateral=False),
        ),
    )
    chain = ChainPortfolio(ChainWallet(BASE), ChainLiquidity(BASE), lending)
    assert chain.lending_net_usd() == Decimal(80500)


def test_an_unreadable_section_makes_the_total_a_lower_bound_and_is_named() -> None:
    read = _chain(holdings=(Held("ETH", Decimal(1), ETH_USD),))
    unread = _chain(chain=ARBITRUM, unread=Section.LENDING)

    grand = total([WalletPortfolio(WALLET, (read, unread))])

    assert grand.usd == ETH_USD
    assert not grand.complete
    assert grand.missing == ((ARBITRUM, Section.LENDING),)


def test_something_with_no_price_is_named_and_not_summed() -> None:
    chain = _chain(
        holdings=(Held("ETH", Decimal(1), ETH_USD), Held("FOO", Decimal(10), None)),
        positions=(_position(usd=None),),
    )
    assert chain.usd() == ETH_USD
    assert chain.unpriced() == ("FOO", "Uniswap v3 #4558452")


def test_two_wallets_are_summed_with_a_subtotal_each() -> None:
    first = WalletPortfolio(WALLET, (_chain(holdings=(Held("ETH", Decimal(1), ETH_USD),)),))
    second = WalletPortfolio(SECOND, (_chain(holdings=(Held("USDC", Decimal(50), Decimal(1)),)),))

    text = render_portfolio([first, second], Language.ENGLISH)

    assert "__Wallet …3d68__ — $2,000.00" in text
    assert "__Wallet …75e0__ — $50.00" in text
    assert "**Total ≈ $2,050.00**" in text
    # Only the header, which the verbatim answer drops, spells an address out.
    body = text.split("\n", 1)[1]
    assert WALLET not in body and SECOND not in body


# --- the answer -------------------------------------------------------------------


def test_portuguese_figures_and_a_lower_bound_name_the_chain() -> None:
    wallet = WalletPortfolio(
        WALLET,
        (
            _chain(holdings=(Held("USDC", Decimal("1234.5"), Decimal(1)),)),
            _chain(chain=ARBITRUM, unread=Section.LENDING),
        ),
    )

    text = render_portfolio([wallet], Language.PORTUGUESE)

    assert "**Base** — US$ 1.234,50" in text
    assert "Carteira US$ 1.234,50 · 1.234,50 USDC" in text
    assert "**Arbitrum** — pelo menos US$ 0,00" in text
    assert "**Pelo menos US$ 1.234,50** — não lido: Arbitrum (Aave)" in text
    assert "Total ≈" not in text


def test_chains_holding_nothing_share_one_line_and_fees_are_on_the_liquidity_line() -> None:
    wallet = WalletPortfolio(
        WALLET,
        (
            _chain(chain=ETHEREUM),
            _chain(positions=(_position(fees0="0.01"),)),
            _chain(chain=ARBITRUM),
        ),
    )

    text = render_portfolio([wallet], Language.ENGLISH)

    assert "**Ethereum**, **Arbitrum** — nothing held" in text
    assert (
        "Liquidity $3,020.00 (incl. $20.00 uncollected fees) · 1 Uniswap position, 1 in range"
    ) in text


@pytest.mark.parametrize(
    ("question", "language"),
    [
        ("quanto eu tenho no total?", Language.PORTUGUESE),
        ("meu portfólio 0xabc", Language.PORTUGUESE),
        ("what is my portfolio worth?", Language.ENGLISH),
        (WALLET, Language.ENGLISH),
    ],
)
def test_the_answer_is_in_the_language_of_the_question(question: str, language: Language) -> None:
    assert portfolio_language(question) is language


# --- the reads ----------------------------------------------------------------------


class Recording:
    """The fake Base node, and every call it was sent, multicalls unfolded."""

    def __init__(self, chain: FakeChain) -> None:
        self.chain = chain
        self.calls: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "base-mainnet.infura.io":
            return json_rpc(request)
        body = json.loads(request.content)
        for call in body if isinstance(body, list) else [body]:
            params = call.get("params") or [{}]
            if call.get("method") == "eth_call":
                self._record(params[0]["to"].lower(), params[0]["data"])
        return self.chain.handle(request)

    def _record(self, target: str, data: str) -> None:
        if target == "0xca11bde05977b3631167028862be2a173976ca11":
            self.calls.extend(decode_aggregate3_call(data))
        else:
            self.calls.append((target, data))

    def balance_reads(self) -> set[str]:
        return {target for target, data in self.calls if data.startswith(abi.BALANCE_OF)}


def base_chain() -> FakeChain:
    chain = FakeChain(DEPLOYMENTS[1])
    chain.native[WALLET] = Decimal("0.5")
    chain.balances[(USDC, WALLET)] = Decimal(100)
    chain.balances[(CBBTC, WALLET)] = Decimal("0.001")
    chain.prices.update({WETH: ETH_USD, USDC: Decimal(1), CBBTC: BTC_USD})
    chain.lending[(WALLET, WETH)] = AaveReserve(Decimal(1))
    chain.lending[(WALLET, USDC)] = AaveReserve(borrowed=Decimal(500))
    chain.lending[(WALLET, CBBTC)] = AaveReserve(Decimal("0.01"), collateral=False)
    return chain


class FixedPrice:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def usd_price(self, symbol: str) -> PricedAsset | None:
        self.asked.append(symbol)
        return PricedAsset(symbol, Decimal(1900), "2026-09-23 20:17Z")


def _provider(recording: Recording, prices: FixedPrice | None = None) -> PositionsProvider:
    return PositionsProvider(
        DEPLOYMENTS,
        "e2e-infura",
        CallBudget(5),
        transport=httpx.MockTransport(recording.handle),
        prices=prices,
    )


async def _base(recording: Recording, *addresses: str, prices: FixedPrice | None = None) -> str:
    cleared = Cleared(addresses or (WALLET,), "what is my portfolio worth?")
    return await _provider(recording, prices).portfolio(cleared)


async def test_aave_reserve_assets_are_read_so_a_token_outside_the_named_set_is_found() -> None:
    recording = Recording(base_chain())

    text = await _base(recording)

    assert "0.001 cbBTC" in text
    # 1,000 of ether, 100 USDC and 80 of cbBTC in the wallet; 2,000 - 500 +
    # 800 on Aave.
    assert "**Total ≈ $3,480.00**" in text


async def test_only_underlying_tokens_are_read_never_an_atoken_or_debt_token() -> None:
    """Every `balanceOf` goes to a named token or a reserve's underlying: an
    Aave position is counted once, in the Aave section."""
    recording = Recording(base_chain())

    await _base(recording)

    named = {t.contract.lower() for t in tokens_for(BASE)}
    # Position NFTs are counted with `balanceOf` too, on their managers.
    base = DEPLOYMENTS[1]
    managers = {base.v3_position_manager, base.v4_position_manager}
    reads = recording.balance_reads()
    assert CBBTC.lower() in reads, "a reserve's underlying token is read"
    assert reads - managers <= named | set(recording.chain.reserves)


async def test_a_section_that_fails_twice_is_named_and_the_rest_still_counted() -> None:
    chain = base_chain()
    chain.failing_selectors.add(abi.AAVE_ACCOUNT_DATA)
    recording = Recording(chain)

    text = await _base(recording)

    assert "**At least $1,180.00** — not read: Base (Aave)" in text
    tries = [c for c in recording.calls if c[1].startswith(abi.AAVE_ACCOUNT_DATA)]
    assert len(tries) == 2


async def test_a_section_that_fails_once_is_read_on_the_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = base_chain()
    chain.failing_selectors.add(abi.AAVE_ACCOUNT_DATA)
    recording = Recording(chain)
    original = chain.handle

    def recover(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if abi.AAVE_ACCOUNT_DATA.encode() in request.content:
            chain.failing_selectors.clear()
        return response

    monkeypatch.setattr(chain, "handle", recover)

    text = await _base(recording)

    assert "**Total ≈ $3,480.00**" in text


async def test_two_wallets_share_one_node_and_one_contract_lookup_per_chain() -> None:
    chain = base_chain()
    chain.native[SECOND] = Decimal(1)
    recording = Recording(chain)

    text = await _base(recording, WALLET, SECOND)

    assert "__Wallet …75e0__ — $2,000.00" in text
    assert "**Total ≈ $5,480.00**" in text
    pool_lookups = [c for c in recording.calls if c[1].startswith(abi.AAVE_GET_POOL)]
    assert len(pool_lookups) == 1


async def test_ether_is_priced_by_coingecko_only_when_the_oracle_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = base_chain()
    chain.failing_selectors.add(abi.AGGREGATE3)
    prices = FixedPrice()
    recording = Recording(chain)
    # Only the oracle's multicall fails: let every other one through.
    original = chain.handle

    def oracle_down(request: httpx.Request) -> httpx.Response:
        oracle = abi.AAVE_ASSET_PRICE.removeprefix("0x").encode()
        failing = oracle in request.content
        chain.failing_selectors = {abi.AGGREGATE3} if failing else set()
        return original(request)

    monkeypatch.setattr(chain, "handle", oracle_down)

    text = await _base(recording, prices=prices)

    assert prices.asked and set(prices.asked) == {"ETH"}
    assert "ETH from CoinGecko" in text


async def test_the_reserve_list_is_read_once_per_process() -> None:
    recording = Recording(base_chain())
    provider = _provider(recording)
    cleared = Cleared((WALLET,), "what is my portfolio worth?")

    await provider.portfolio(cleared)
    await provider.portfolio(cleared)

    lists = [c for c in recording.calls if c == (AAVE_POOL, abi.AAVE_RESERVES_LIST)]
    # Once for the wallet's reserves; `lending` reads its own each time.
    wallet_reads = len(lists) - 2
    assert wallet_reads == 1


def test_a_cached_reserve_list_expires() -> None:
    now = [0.0]
    cache = ReserveCache(ttl_seconds=10, clock=lambda: now[0])
    cache.put("base:pool", (USDC_INFO,))
    assert cache.get("base:pool") == (USDC_INFO,)
    now[0] = 10.0
    assert cache.get("base:pool") is None


# --- the clearance ---------------------------------------------------------------------


def _clearance(text: str, question: str) -> AuthorizedQuery:
    return EgressGuard().authorize(
        EgressRequest(
            asker=ASKER,
            query=ProvenancedQuery(text=text, origin=QueryOrigin.ASKER, question=question),
            provider=DEFI_POSITIONS_PROVIDER,
        )
    )


async def _clear(text: str, question: str, limit: int = MAX_ADDRESSES) -> Cleared | ToolResult:
    with authorized(_clearance(text, question)):
        return await clear_addresses(
            DEFI_POSITIONS_PROVIDER, PORTFOLIO_TOOL, CallBudget(3), RateLimiter(0), limit=limit
        )


async def test_every_address_the_asker_wrote_is_cleared() -> None:
    cleared = await _clear(f"{WALLET} {SECOND}", f"my portfolio {WALLET} and {SECOND}")
    assert isinstance(cleared, Cleared)
    assert cleared.addresses == (WALLET, SECOND)
    assert cleared.question == f"my portfolio {WALLET} and {SECOND}"


async def test_an_address_beside_a_word_is_refused_not_trimmed() -> None:
    refused = await _clear(f"{WALLET} portfolio", f"my portfolio {WALLET}")
    assert isinstance(refused, ToolResult)
    assert "not_an_address" in refused.text


async def test_more_wallets_than_a_person_has_is_refused() -> None:
    # A lower limit, because past five addresses the 256-character query
    # bound refuses first.
    many = [f"0x{i:040x}" for i in range(1, 4)]
    refused = await _clear(" ".join(many), "portfolio " + " ".join(many), limit=2)
    assert isinstance(refused, ToolResult)
    assert "too_many_addresses" in refused.text


async def test_every_wallet_a_person_may_save_fits_one_portfolio_call() -> None:
    assert MAX_ADDRESSES == MAX_WALLETS_PER_KIND
    five = [f"0x{i:040x}" for i in range(1, MAX_ADDRESSES + 1)]
    cleared = await _clear(" ".join(five), "my portfolio " + " ".join(five))
    assert isinstance(cleared, Cleared)
    assert len(cleared.addresses) == MAX_ADDRESSES


def test_an_address_the_asker_did_not_write_never_gets_a_clearance() -> None:
    with pytest.raises(EgressRefused):
        _clearance(f"{WALLET} {SECOND}", f"my portfolio {WALLET}")


# --- back-off on the balances path ---------------------------------------------------------


async def test_a_rate_limited_balance_read_is_retried_rather_than_failing_the_chain() -> None:
    """Regression: `ChainReader` had no back-off, so a balance read straight
    after a positions read came back "could not be reached" on Arbitrum,
    and the same read a moment later answered."""
    attempts: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        body = json.loads(request.content)
        if len(attempts) == 1:
            limited = {"jsonrpc": "2.0", "id": 0, "error": {"code": -32005}}
            return httpx.Response(200, json=[limited])
        return httpx.Response(
            200, json=[{"jsonrpc": "2.0", "id": c["id"], "result": "0x0"} for c in body]
        )

    reader = ChainReader(
        ARBITRUM, "key", transport=httpx.MockTransport(handle), backoff=(0,)
    )
    result = await reader.balances(WALLET, ())

    assert result.ok
    assert len(attempts) == 2


def test_one_rate_limited_entry_marks_a_whole_batch() -> None:
    limited = httpx.Response(
        200, json=[{"id": 0, "result": "0x0"}, {"id": 1, "error": {"code": -32005}}]
    )
    fine = httpx.Response(200, json=[{"id": 0, "result": "0x0"}])
    assert rate_limited(limited)
    assert not rate_limited(fine)


async def test_the_native_balance_is_read_through_the_node() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["method"] == "eth_getBalance"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(10**18)})

    node = Node("https://x", httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert await node.native_balance(WALLET) == 10**18
