"""Price alerts and near-edge warnings, asked for in words and fired by the sweep.

The assembled process with alerts on. A price request reads one constant
CoinGecko request through `Edges.http_transport` -- no wallet -- or, when
CoinGecko refuses, one constant read of the Chainlink feeds on mainnet, and
the sweep reads it again, once for every price alert, and messages on the
crossing in the language the alert was asked in. A near-edge request is a
range alert with a distance: its confirmation shows how far the nearer edge
is now, and the sweep warns once when the position comes that close.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.discord.bot import PLATFORM
from chatmemory.app.alerts import AlertRunner
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.facts import FactKind
from tests.e2e.harness.chain import FakeChain, FakeFeeds, V3Position
from tests.e2e.harness.conversation import E2EBot, Turn
from tests.e2e.harness.discord_wire import Sent
from tests.e2e.harness.process import FakeClock, e2e_settings, start
from tests.e2e.harness.web import NetworkSeal

COINGECKO = "api.coingecko.com"
COINGECKO_KEY = SecretStr("e2e-coingecko-key")
MAINNET = "mainnet.infura.io"
BASE_HOST = "base-mainnet.infura.io"
WALLET = "0xdd8a0000000000000000000000000000000063d6"
POOL = "0xd0b53d9277642d899df5c87a3966a349a798f224"
TOKEN_ID = 4558452
SWEEP = timedelta(minutes=5)
QUOTED_AT = 1788264000  # 2026-09-01 12:00 UTC


@pytest_asyncio.fixture
async def alerts_bot(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """The production process with `ALERTS_ENABLED=true` and a CoinGecko key,
    so prices are asked of CoinGecko first; market tools stay off."""
    settings = e2e_settings(e2e_database_url).model_copy(
        update={"alerts_enabled": True, "coingecko_api_key": COINGECKO_KEY}
    )
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


class Market:
    """CoinGecko's simple price endpoint, with a price a scenario can move."""

    def __init__(self, btc: str, eth: str = "2480.50") -> None:
        self.usd = {"bitcoin": Decimal(btc), "ethereum": Decimal(eth)}
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                coin: {"usd": float(usd), "last_updated_at": QUOTED_AT}
                for coin, usd in self.usd.items()
            },
        )


def sweeper(bot: E2EBot) -> tuple[AlertRunner, FakeClock]:
    runner = bot.process.graph.alerts
    assert runner is not None
    return runner, cast(FakeClock, bot.process.edges.clock)


async def sweep(bot: E2EBot) -> Turn:
    runner, clock = sweeper(bot)
    clock.advance(SWEEP)
    calls = len(bot.chat.calls)
    turn = await bot.turn(lambda: runner.run_due(clock()))
    assert len(bot.chat.calls) == calls and not turn.searched
    return turn


async def rows(bot: E2EBot) -> list[Any]:
    async with bot.engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT id, kind, chain, address, address_source, asset, direction, "
                "price_level, edge_percent, language, state, token_id "
                "FROM position_alert ORDER BY id"
            )
        )
        return list(found)


def prompt(turn: Turn) -> Sent:
    [sent] = [s for s in turn.sent if s.buttons]
    return sent


async def test_a_price_alert_is_asked_confirmed_and_fires_once_in_portuguese(
    alerts_bot: E2EBot,
) -> None:
    bot = alerts_bot
    market = Market(btc="97412.35")
    bot.web.script(COINGECKO, market.handle)
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    # Known to the store, as anybody who has spoken to the bot is; no wallet
    # is involved in a price alert.
    await bot.process.facts.remember(
        Viewer(PersonRef(PLATFORM, leo.id), frozenset()), FactKind.ETH_WALLET, WALLET
    )
    calls = len(bot.chat.calls)

    asked = await dm.say("avisa quando o BTC passar de 100k")

    assert not asked.searched and len(bot.chat.calls) == calls
    assert asked.hosts == {COINGECKO}, "a price alert reads the price, never a chain"
    offer = prompt(asked)
    assert "• **BTC** acima de **US$ 100.000** · agora **US$ 97.412,35** (CoinGecko," in (
        offer.content
    )
    assert WALLET not in offer.content
    asked.assert_language("pt")
    assert await rows(bot) == []

    confirmed = await dm.press(offer, "Confirmar")
    [done] = confirmed.sent
    assert done.content.startswith("Pronto. Estou acompanhando:")
    [row] = await rows(bot)
    assert (row.kind, row.chain, row.address, row.address_source) == ("price", None, None, None)
    assert (row.asset, row.direction, row.price_level) == ("BTC", "above", Decimal(100000))
    assert (row.language, row.state) == ("pt", "below")
    assert f"**{row.id}** — BTC acima de US$ 100.000" in done.content

    before = len(market.requests)
    market.usd["bitcoin"] = Decimal("100412.35")
    crossed = await sweep(bot)

    [message] = crossed.sent
    assert message.via == "dm"
    assert message.content.startswith(
        "🔔 **Alerta** — o BTC **passou de US$ 100.000**: agora US$ 100.412,35 "
        "(CoinGecko, 2026-09-01 12:00 UTC)."
    )
    assert f"`/alert delete {row.id}`" in message.content
    crossed.assert_language("pt")
    assert crossed.hosts == {COINGECKO} and len(market.requests) == before + 1

    assert (await sweep(bot)).sent == (), "still above is not news"
    [after] = await rows(bot)
    assert after.state == "above"

    listed = await dm.slash("alert list", locale="pt-BR")
    assert f"**{row.id}** - BTC acima de US$ 100.000 - acima do nível" in listed.text


def coingecko_refused(request: httpx.Request) -> httpx.Response:
    """The CloudFront 403 CoinGecko's price endpoint answered on 2026-09-29."""
    return httpx.Response(
        403,
        text="<HTML><TITLE>ERROR: The request could not be satisfied</TITLE>403 ERROR</HTML>",
        headers={"content-type": "text/html"},
    )


async def test_with_coingecko_refusing_a_price_alert_is_offered_and_fires_from_chainlink(
    alerts_bot: E2EBot,
) -> None:
    bot = alerts_bot
    refusals: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        refusals.append(request)
        return coingecko_refused(request)

    feeds = FakeFeeds(prices={"BTC": Decimal("97412.35"), "ETH": Decimal("2480.50")})
    bot.web.script(COINGECKO, refuse)
    bot.web.script(MAINNET, feeds.handle)
    leo = bot.person("Leo")
    # Known to the store, as anybody who has spoken to the bot is.
    await bot.process.facts.remember(
        Viewer(PersonRef(PLATFORM, leo.id), frozenset()), FactKind.ETH_WALLET, WALLET
    )
    dm = bot.dm(leo)

    asked = await dm.say("avisa quando o BTC passar de 100k")

    assert asked.hosts == {COINGECKO, MAINNET}, "CoinGecko first, then the feed"
    assert all(
        r.headers["x-cg-demo-api-key"] == COINGECKO_KEY.get_secret_value() for r in refusals
    )
    offer = prompt(asked)
    assert "• **BTC** acima de **US$ 100.000** · agora **US$ 97.412,35** (Chainlink," in (
        offer.content
    )
    asked.assert_language("pt")
    confirmed = await dm.press(offer, "Confirmar")
    assert confirmed.sent[0].content.startswith("Pronto. Estou acompanhando:")
    [row] = await rows(bot)

    reads = len(feeds.requests)
    feeds.prices["BTC"] = Decimal("100412.35")
    feeds.default_updated_at = QUOTED_AT + 240  # 12:04 UTC, a minute before the sweep
    crossed = await sweep(bot)

    [message] = crossed.sent
    assert message.via == "dm"
    assert message.content.startswith(
        "🔔 **Alerta** — o BTC **passou de US$ 100.000**: agora US$ 100.412,35 "
        "(Chainlink, 2026-09-01 12:04 UTC)."
    )
    assert f"`/alert delete {row.id}`" in message.content
    crossed.assert_language("pt")
    assert crossed.hosts == {COINGECKO, MAINNET} and len(feeds.requests) == reads + 1


def base_chain() -> FakeChain:
    chain = FakeChain(DEPLOYMENTS[1])
    chain.v3[TOKEN_ID] = V3Position(WALLET, POOL, -199560, -195770, 10**15)
    chain.ticks[POOL] = -197404
    return chain


async def test_a_near_edge_warning_is_confirmed_and_fires_once(alerts_bot: E2EBot) -> None:
    bot = alerts_bot
    chain = base_chain()
    bot.web.script(BASE_HOST, chain.handle)
    leo = bot.person("Leo")
    await bot.process.facts.remember(
        Viewer(PersonRef(PLATFORM, leo.id), frozenset()), FactKind.ETH_WALLET, WALLET
    )
    dm = bot.dm(leo)

    asked = await dm.say("warn me when my LP on base is within 10% of the range edge")

    offer = prompt(asked)
    assert "Uniswap v3 WETH/USDC 0.05% #4558452" in offer.content
    # Tick -197404 in [-199560, -195770): 1634 ticks under the top.
    assert "**17.7% from the upper edge**; I'll warn you within 10%" in offer.content
    await dm.press(offer, "Confirm")
    [row] = await rows(bot)
    assert (row.kind, row.token_id, row.edge_percent, row.state) == (
        "lp_range", TOKEN_ID, Decimal(10), "in_range"
    )

    assert (await sweep(bot)).sent == ()
    chain.ticks[POOL] = -196270  # 500 ticks under the top: about 5.1%
    assert (await sweep(bot)).sent == (), "one read near the edge is not yet a change"
    warned = await sweep(bot)
    [message] = warned.sent
    assert message.content.startswith(
        "🔔 **Alert** — your Uniswap v3 WETH/USDC 0.05% position #4558452 on Base is "
        "**5.1% from the upper edge** of its range"
    )
    assert (await sweep(bot)).sent == (), "still near the edge is not news"
    [after] = await rows(bot)
    assert after.state == "near_edge"
