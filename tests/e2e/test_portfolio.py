"""Portfolio totals, end to end: "quanto eu tenho no total?" answered from the chain.

Before this route the question went to the corpus: it names no wallet and no
position, so neither chain predicate claimed it, and retrieval answered from a
colleague's message about their project's treasury. Here the whole process
runs: routing, the tool proposal, the egress guard admitting the saved wallet,
the real positions provider reading the fake nodes, and the reply on the wire.

Base holds the figures, reduced from a live read of a real wallet: ether and
USDC in the wallet, cbBTC that only the Aave reserve list finds, and an Aave
account with a cbBTC supply that is not collateral -- which the account's
collateral-minus-debt would leave out. Ethereum and Arbitrum are the null node.
"""

from __future__ import annotations

from decimal import Decimal

import discord
import httpx

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.discord.bot import PLATFORM
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.facts import FactKind
from tests.e2e.conftest import COLLEAGUE_CRYPTO
from tests.e2e.harness.chain import CBBTC, USDC, WETH, AaveAccount, AaveReserve, FakeChain
from tests.e2e.harness.conversation import E2EBot

BASE_HOST = "base-mainnet.infura.io"
WALLET = "0xdd8ac30cb0a219af4963eab5e30ed065b5e63d68"
TYPED = "0xb26b9a0d4fa4f2a1b7f6dd1c3b9a1e2f5c3d75e0"

ETH_USD = Decimal("2672.57")
USDC_USD = Decimal("0.99997645")
CBBTC_USD = Decimal("84332.51")

WALLET_USD = (
    Decimal("0.00025") * ETH_USD
    + Decimal("69.133113") * USDC_USD
    + Decimal("0.00012054") * CBBTC_USD
)
AAVE_NET_USD = (
    Decimal("0.8386") * ETH_USD
    + (Decimal("14733.86") - Decimal("15221.48")) * USDC_USD
    + Decimal("0.12717") * CBBTC_USD
)


def base_chain() -> FakeChain:
    chain = FakeChain(DEPLOYMENTS[1])
    chain.native[WALLET] = Decimal("0.00025")
    chain.balances[(USDC, WALLET)] = Decimal("69.133113")
    chain.balances[(CBBTC, WALLET)] = Decimal("0.00012054")
    chain.prices.update({WETH: ETH_USD, USDC: USDC_USD, CBBTC: CBBTC_USD})
    chain.lending[(WALLET, WETH)] = AaveReserve(Decimal("0.8386"))
    chain.lending[(WALLET, USDC)] = AaveReserve(Decimal("14733.86"), Decimal("15221.48"))
    chain.lending[(WALLET, CBBTC)] = AaveReserve(Decimal("0.12717"), collateral=False)
    chain.aave[WALLET] = AaveAccount(Decimal("16974.73"), Decimal("15221.12"), Decimal("1.43"))
    return chain


def pt_usd(value: Decimal) -> str:
    return "US$ " + f"{value:,.2f}".translate(str.maketrans(",.", ".,"))


async def save_wallet(bot: E2EBot, who: discord.Member) -> None:
    person = PersonRef(PLATFORM, who.id)
    await bot.process.facts.remember(Viewer(person, frozenset()), FactKind.ETH_WALLET, WALLET)


async def test_quanto_eu_tenho_no_total_sums_every_chain_and_never_searches(
    bot: E2EBot,
) -> None:
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    bot.web.script(BASE_HOST, base_chain().handle)

    turn = await bot.dm(leo).say("quanto eu tenho no total?")

    assert turn.edge() == "CHAIN"
    assert not turn.searched, "a portfolio question must never search the corpus"
    assert turn.schemas == ("tools",), "one tool proposal, and no model writes the figures"
    [reply] = turn.sent
    text = reply.content
    assert f"**Base** — {pt_usd(WALLET_USD + AAVE_NET_USD)}" in text
    assert "0,00012054 cbBTC" in text, "cbBTC is found through the Aave reserve list"
    assert f"Aave líquido {pt_usd(AAVE_NET_USD)}" in text
    assert "**Ethereum**, **Arbitrum** — nada" in text
    assert f"**Total ≈ {pt_usd(WALLET_USD + AAVE_NET_USD)}**" in text
    assert "Não incluído:" in text
    assert COLLEAGUE_CRYPTO not in text
    turn.assert_language("pt")


async def test_a_chain_that_cannot_be_read_makes_the_total_a_lower_bound(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    chain = base_chain()
    chain.failing_selectors.add("0xbf92857c")  # getUserAccountData, on both attempts
    bot.web.script(BASE_HOST, chain.handle)

    turn = await bot.dm(leo).say("what is my portfolio worth?")

    assert turn.edge() == "CHAIN" and not turn.searched
    assert f"**At least ${WALLET_USD:,.2f}** — not read: Base (Aave)" in turn.text
    assert "Total ≈" not in turn.text, "a partial figure is never stated as the total"
    reads = [
        r for r in bot.web.calls
        if r.url.host == BASE_HOST and b"0xbf92857c" in r.content
    ]
    assert len(reads) == 2, "the failed section is tried once more, and only once"


async def test_without_a_saved_wallet_the_address_is_asked_for(bot: E2EBot) -> None:
    turn = await bot.dm(bot.person("Ana")).say("quanto eu tenho no total?")

    assert turn.edge() == "NONE"
    assert not turn.searched
    assert turn.text.startswith("Qual carteira?")
    turn.assert_language("pt")


async def test_e_no_total_after_a_balance_question_sums_that_wallet(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    bot.web.script(BASE_HOST, base_chain().handle)

    first = await dm.say(f"qual o saldo da carteira {WALLET}?")
    assert first.edge() == "CHAIN"

    follow_up = await dm.say("e no total?")

    assert follow_up.edge() == "CHAIN"
    assert not follow_up.searched
    assert f"**Total ≈ {pt_usd(WALLET_USD + AAVE_NET_USD)}**" in follow_up.text


async def test_in_a_channel_no_full_address_is_posted(bot: E2EBot) -> None:
    """The citation footer quotes the tool's header; it once named every
    wallet in full, the saved one included, to the whole channel."""
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    bot.web.script(BASE_HOST, base_chain().handle)

    turn = await bot.channel("general", leo).say(f"what is my portfolio worth with {TYPED}?")

    assert turn.edge() == "CHAIN" and not turn.searched
    assert "…3d68" in turn.text and "…75e0" in turn.text
    for address in (WALLET, TYPED):
        assert address[2:].lower() not in turn.text.lower()


async def test_ether_is_priced_by_coingecko_when_the_oracle_is_down(bot: E2EBot) -> None:
    """The production wiring hands the positions provider a price lookup;
    without it, ether in a portfolio goes unpriced whenever the oracle fails."""
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    chain = base_chain()
    oracle = abi.AAVE_ASSET_PRICE.removeprefix("0x").encode()

    def oracle_down(request: httpx.Request) -> httpx.Response:
        chain.failing_selectors = {abi.AGGREGATE3} if oracle in request.content else set()
        return chain.handle(request)

    bot.web.script(BASE_HOST, oracle_down)
    bot.web.script(
        "api.coingecko.com",
        lambda _: httpx.Response(200, json={"ethereum": {"usd": float(ETH_USD)}}),
    )

    turn = await bot.dm(leo).say("what is my portfolio worth?")

    assert turn.edge() == "CHAIN" and not turn.searched
    assert "ETH from CoinGecko" in turn.text
    assert any(r.url.host == "api.coingecko.com" for r in bot.web.calls)
