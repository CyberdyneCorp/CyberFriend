"""A preferred currency, end to end: said once, then beside every dollar figure.

The whole process runs: the introduction is recognised and saved, the price
question goes through the market route and the egress guard to CoinGecko, the
portfolio through the positions provider to the fake nodes -- and the rate
through the FX provider to Frankfurter, the only other host a second figure
may reach. A failing rate host leaves the answer in dollars alone.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.e2e.harness.chain import V3Position
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.process import e2e_settings, start
from tests.e2e.harness.web import NetworkSeal
from tests.e2e.test_portfolio import (
    AAVE_NET_USD,
    BASE_HOST,
    WALLET,
    WALLET_USD,
    base_chain,
    pt_usd,
    save_wallet,
)

FX_HOST = "api.frankfurter.dev"
COINGECKO = "api.coingecko.com"
BRL = Decimal("5.46")
BTC_USD = Decimal("63210")
LP_TOKEN = 4242
LP_POOL = "0x" + "d" * 40


def pt_brl(value: Decimal) -> str:
    return "R$ " + f"{value:,.2f}".translate(str.maketrans(",.", ".,"))


def fx(request: httpx.Request) -> httpx.Response:
    assert dict(request.url.params) == {"from": "USD", "to": "BRL"}, "two codes, no amount"
    return httpx.Response(200, json={"base": "USD", "date": "2026-09-01", "rates": {"BRL": 5.46}})


def fx_down(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503)


def bitcoin(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "bitcoin": {"usd": 63210, "last_updated_at": 1788263940},
            "ethereum": {"usd": 2672.57, "last_updated_at": 1788263940},
        },
    )


@pytest_asyncio.fixture
async def market_bot(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """The production process with the market tools on."""
    settings = e2e_settings(e2e_database_url).model_copy(
        update={"market_tools_enabled": True}
    )
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


async def test_an_introduction_with_a_currency_saves_it_and_confirms_in_portuguese(
    bot: E2EBot,
) -> None:
    leo = bot.person("Leo")

    turn = await bot.dm(leo).say("Oi, me chamo Leo, moro no Brasil e uso reais")

    assert turn.edge() == "NONE" and not turn.searched
    assert "✓ Moeda preferida: **real brasileiro (BRL)**" in turn.text
    assert (await bot.facts_of(leo))["preferred_currency"] == "BRL"
    turn.assert_language("pt")

    shown = await bot.dm(leo).say("o que você sabe sobre mim?")
    assert "• Moeda preferida: **real brasileiro (BRL)**" in shown.text


async def test_an_unsupported_currency_is_refused_with_the_supported_ones(bot: E2EBot) -> None:
    leo = bot.person("Leo")

    turn = await bot.dm(leo).say("minha moeda é o peso argentino")

    assert "Não salvei isso como sua moeda preferida" in turn.text and "BRL" in turn.text
    assert "preferred_currency" not in await bot.facts_of(leo)


async def test_the_bitcoin_price_is_shown_in_dollars_and_reais(market_bot: E2EBot) -> None:
    bot = market_bot
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.say("prefiro ver em reais")
    bot.web.script(COINGECKO, bitcoin)
    bot.web.script(FX_HOST, fx)

    turn = await dm.say("qual o preço do bitcoin?")

    assert turn.edge() == "MARKET" and not turn.searched
    assert f"Bitcoin (BTC): 63,210 USD ({pt_brl(BTC_USD * BRL)})" in turn.text
    assert "BRL pela taxa de referência diária: 1 USD = 5,4600 BRL" in turn.text
    fx_calls = [r for r in bot.web.calls if r.url.host == FX_HOST]
    assert len(fx_calls) == 1


async def test_a_cached_price_is_converted_again_for_each_asker(market_bot: E2EBot) -> None:
    """The second question inside the quote's freshness reads no new price, and
    still gets the figure its own asker wants: reais for Leo, dollars for Ana."""
    bot = market_bot
    leo, ana = bot.person("Leo"), bot.person("Ana")
    await bot.dm(leo).say("prefiro ver em reais")
    bot.web.script(COINGECKO, bitcoin)
    bot.web.script(FX_HOST, fx)
    converted = f"Bitcoin (BTC): 63,210 USD ({pt_brl(BTC_USD * BRL)})"

    first = await bot.dm(leo).say("qual o preço do bitcoin?")
    again = await bot.dm(leo).say("e o preço do bitcoin agora?")
    other = await bot.dm(ana).say("qual o preço do bitcoin?")

    assert converted in first.text and converted in again.text
    assert "Bitcoin (BTC): 63,210 USD" in other.text
    assert "R$" not in other.text, "one asker's currency never reaches another"
    assert len([r for r in bot.web.calls if r.url.host == COINGECKO]) == 1, "served cached"


async def test_with_the_rate_host_failing_the_price_is_in_dollars_only(
    market_bot: E2EBot,
) -> None:
    bot = market_bot
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.say("prefiro ver em reais")
    bot.web.script(COINGECKO, bitcoin)
    bot.web.script(FX_HOST, fx_down)

    turn = await dm.say("qual o preço do bitcoin?")

    assert turn.edge() == "MARKET"
    assert "Bitcoin (BTC): 63,210 USD\n" in turn.text + "\n"
    assert "R$" not in turn.text and "BRL" not in turn.text


async def test_the_portfolio_shows_both_until_the_preference_is_forgotten(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await save_wallet(bot, leo)
    await dm.say("moeda preferida: BRL")
    bot.web.script(BASE_HOST, base_chain().handle)
    bot.web.script(FX_HOST, fx)
    worth = WALLET_USD + AAVE_NET_USD

    turn = await dm.say("quanto eu tenho no total?")

    assert turn.edge() == "CHAIN" and not turn.searched
    assert f"**Total ≈ {pt_usd(worth)} ({pt_brl(worth * BRL)})**" in turn.text
    assert f"Aave líquido {pt_usd(AAVE_NET_USD)} ({pt_brl(AAVE_NET_USD * BRL)})" in turn.text
    assert "BRL pela taxa de referência diária: 1 USD = 5,4600 BRL" in turn.text
    assert len([r for r in bot.web.calls if r.url.host == FX_HOST]) == 1, "one rate per answer"
    turn.assert_language("pt")

    await dm.slash("forget", scope="everywhere")
    await save_wallet(bot, leo)
    after = await dm.say("quanto eu tenho no total?")

    assert f"**Total ≈ {pt_usd(worth)}**" in after.text
    assert "R$" not in after.text


async def test_with_the_rate_host_failing_the_portfolio_is_in_dollars_only(
    bot: E2EBot,
) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await save_wallet(bot, leo)
    await dm.say("prefiro ver em reais")
    bot.web.script(BASE_HOST, base_chain().handle)
    bot.web.script(FX_HOST, fx_down)

    turn = await dm.say("quanto eu tenho no total?")

    assert f"**Total ≈ {pt_usd(WALLET_USD + AAVE_NET_USD)}**" in turn.text
    assert "R$" not in turn.text


async def test_forgetting_the_currency_by_name_removes_only_it(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.say("Oi, me chamo Leo, moro no Brasil e uso reais")

    turn = await dm.say("esqueça minha moeda")

    assert turn.text == "Pronto. Não tenho mais sua moeda preferida."
    facts = await bot.facts_of(leo)
    assert "preferred_currency" not in facts and facts["preferred_name"] == "Leo"


async def test_liquidity_and_aave_positions_show_both_figures(bot: E2EBot) -> None:
    """The positions answer, not only the portfolio: every dollar value in the
    Uniswap and Aave lines is followed by reais, and the rate is read once."""
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.say("prefiro ver em reais")
    chain = base_chain()
    chain.v3[LP_TOKEN] = V3Position(WALLET, LP_POOL, -199560, -195770, 10**15)
    chain.ticks[LP_POOL] = -197404
    bot.web.script(BASE_HOST, chain.handle)
    bot.web.script(FX_HOST, fx)

    turn = await dm.say(f"quais as posições de liquidez e no Aave da carteira {WALLET}?")

    assert turn.edge() == "CHAIN" and not turn.searched
    collateral, debt = Decimal("16974.73"), Decimal("15221.12")
    assert (
        f"Collateral ${collateral:,.2f} ({pt_brl(collateral * BRL)}) · "
        f"Debt ${debt:,.2f} ({pt_brl(debt * BRL)})"
    ) in turn.text
    assert re.search(r"Holds: .* ≈ \$[\d,.]+ \(R\$ [\d.]+,\d\d\)", turn.text), "the LP value"
    assert re.search(r"Uncollected: .* ≈ \$[\d,.]+ \(R\$ [\d.]+,\d\d\)", turn.text)
    assert re.search(r"0\.8386 WETH ≈ \$[\d,.]+ \(R\$ [\d.]+,\d\d\)", turn.text), "an asset line"
    assert turn.text.count("BRL pela taxa de referência diária") == 1, "one footnote"
    assert len([r for r in bot.web.calls if r.url.host == FX_HOST]) == 1, "one rate per answer"
