"""Position alerts, end to end: a stored alert, a moving chain, and the DMs.

Creation is a later change, so the alert is written through the store API a
command will use, over a saved wallet. Everything after that is the assembled
process: the sweep `main` would start, the watcher reading the fake node
through `Edges.http_transport`, and the direct message through the same
messenger scheduled tasks use, arriving on the fake Discord wire. The clock is
the process's own `Edges.clock`, moved one sweep at a time.

What each scenario pins is the edge trigger as a person meets it: one message
when a position leaves its range, silence while it stays out, one when it comes
back -- in the language the alert was made in, with no model call and no
corpus search anywhere on the path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import cast

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.discord.bot import PLATFORM
from chatmemory.adapters.store.alerts_postgres import PostgresAlertStore
from chatmemory.app.alerts import AlertRunner, AlertService
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.alerts import (
    AddressSource,
    AlertKind,
    AlertLanguage,
    AlertState,
    LpProtocol,
    LpTarget,
    NewAlert,
)
from chatmemory.ports.facts import FactKind
from tests.e2e.harness.chain import AaveAccount, FakeChain, V3Position
from tests.e2e.harness.conversation import E2EBot, Turn
from tests.e2e.harness.process import FakeClock, e2e_settings, start
from tests.e2e.harness.web import NetworkSeal

BASE = DEPLOYMENTS[1]
BASE_HOST = "base-mainnet.infura.io"
WALLET = "0xdd8a0000000000000000000000000000000063d6"
POOL = "0xd0b53d9277642d899df5c87a3966a349a798f224"
TOKEN_ID = 4558452
IN_RANGE_TICK = -197404
OUT_OF_RANGE_TICK = -195000
SWEEP = timedelta(minutes=5)


@pytest_asyncio.fixture
async def alerts_bot(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """The production process with alerts switched on, as PR-A2's deployment will be."""
    settings = e2e_settings(e2e_database_url).model_copy(update={"alerts_enabled": True})
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


class Sweeps:
    """The alert loop's body, one sweep per call, on the process's clock."""

    def __init__(self, bot: E2EBot) -> None:
        runner = bot.process.graph.alerts
        assert runner is not None, "ALERTS_ENABLED with an Infura key builds the sweep"
        self.runner: AlertRunner = runner
        self.bot = bot
        self.clock = cast(FakeClock, bot.process.edges.clock)

    async def next(self) -> Turn:
        self.clock.advance(SWEEP)
        chat_calls = len(self.bot.chat.calls)
        turn = await self.bot.turn(lambda: self.runner.run_due(self.clock()))
        assert len(self.bot.chat.calls) == chat_calls, "a sweep never calls the model"
        assert not turn.searched, "a sweep never searches the corpus"
        return turn


def base_chain() -> FakeChain:
    chain = FakeChain(BASE)
    chain.v3[TOKEN_ID] = V3Position(WALLET, POOL, -199560, -195770, 10**15)
    chain.ticks[POOL] = IN_RANGE_TICK
    return chain


async def save_wallet(bot: E2EBot, who: PersonRef) -> None:
    """The person saves their wallet, which is also what makes them a known person."""
    await bot.process.facts.remember(Viewer(who, frozenset()), FactKind.ETH_WALLET, WALLET)


async def test_a_range_alert_messages_once_out_and_once_back_in_its_language(
    alerts_bot: E2EBot,
) -> None:
    bot = alerts_bot
    leo = bot.person("Leo")
    person = PersonRef(PLATFORM, leo.id)
    chain = base_chain()
    bot.web.script(BASE_HOST, chain.handle)
    sweeps = Sweeps(bot)
    await save_wallet(bot, person)
    created = await AlertService(PostgresAlertStore(bot.engine)).create(
        NewAlert(
            person=person,
            kind=AlertKind.LP_RANGE,
            chain="base",
            address=WALLET,
            address_source=AddressSource.SAVED,
            language=AlertLanguage.PORTUGUESE,
            lp=LpTarget(LpProtocol.UNISWAP_V3, TOKEN_ID, POOL, "WETH", "USDC", 18, 6, 500),
            state=AlertState.IN_RANGE,
            last_value=Decimal(IN_RANGE_TICK),
        ),
        sweeps.clock(),
    )
    assert created.created

    steady = await sweeps.next()
    assert steady.sent == ()
    assert steady.hosts == {BASE_HOST}
    assert chain.requests == 1, "every alert on a chain is one multicall"

    chain.ticks[POOL] = OUT_OF_RANGE_TICK
    wick = await sweeps.next()
    assert wick.sent == (), "one read out of range is not yet a change"

    out = await sweeps.next()
    [dm] = out.sent
    assert dm.via == "dm" and dm.channel_id == bot.discord.dm_channel_id(leo.id)
    assert dm.content.startswith("🔔 **Alerta** — sua posição Uniswap v3 WETH/USDC 0,05% #4558452")
    assert "**saiu da faixa**" in dm.content
    out.assert_language("pt")

    assert (await sweeps.next()).sent == (), "still out of range is not news"

    chain.ticks[POOL] = IN_RANGE_TICK
    assert (await sweeps.next()).sent == ()
    back = await sweeps.next()
    [returned] = back.sent
    assert "**voltou para a faixa**" in returned.content
    back.assert_language("pt")


async def test_a_health_alert_fires_on_the_first_read_below_and_rearms_past_the_band(
    alerts_bot: E2EBot,
) -> None:
    bot = alerts_bot
    leo = bot.person("Leo")
    person = PersonRef(PLATFORM, leo.id)
    chain = base_chain()
    chain.aave[WALLET] = AaveAccount(Decimal("27699.05"), Decimal("15221.12"), Decimal("1.43"))
    bot.web.script(BASE_HOST, chain.handle)
    sweeps = Sweeps(bot)
    await save_wallet(bot, person)
    created = await AlertService(PostgresAlertStore(bot.engine)).create(
        NewAlert(
            person=person,
            kind=AlertKind.AAVE_HEALTH,
            chain="base",
            address=WALLET,
            address_source=AddressSource.SAVED,
            language=AlertLanguage.ENGLISH,
            threshold=Decimal("1.30"),
            state=AlertState.OK,
            last_value=Decimal("1.43"),
        ),
        sweeps.clock(),
    )
    assert created.created

    def health(value: str) -> None:
        chain.aave[WALLET].health_factor = Decimal(value)

    health("1.28")
    [below] = (await sweeps.next()).sent
    assert "dropped to **1.28** (your limit 1.30)" in below.content

    assert (await sweeps.next()).sent == ()
    health("1.32")
    assert (await sweeps.next()).sent == (), "inside the re-arm band"

    chain.failing = True
    health("1.50")
    assert (await sweeps.next()).sent == (), "a failed read never messages"

    chain.failing = False
    health("1.36")
    [recovered] = (await sweeps.next()).sent
    assert "is back to **1.36** (limit 1.30)" in recovered.content
