"""Asking for an alert in words, confirming it with a button, and managing it.

The assembled process with alerts switched on. A request is recognised before
retrieval, the chain is read once for what it would watch, and the reply lists
that with Confirm and Cancel; only the asker's Confirm creates anything, and
the row it creates is what the sweep then checks and messages about. What is
pinned is what a person meets: the words of the prompt, who may press it, what
lands in `position_alert` (read by SQL), and that no step on the way searches
the corpus or calls a model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.discord.bot import PLATFORM
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.facts import FactKind
from tests.e2e.harness.chain import AaveAccount, FakeChain, V3Position
from tests.e2e.harness.conversation import Conversation, E2EBot, Turn
from tests.e2e.harness.discord_wire import Sent
from tests.e2e.harness.process import FakeClock, e2e_settings, start
from tests.e2e.harness.web import INFURA_HOSTS, NetworkSeal

WALLET = "0xdd8a0000000000000000000000000000000063d6"
POOL = "0xd0b53d9277642d899df5c87a3966a349a798f224"
TOKEN_ID = 4558452
SWEEP = timedelta(minutes=5)


@pytest_asyncio.fixture
async def alerts_bot(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """The production process as it runs with `ALERTS_ENABLED=true`."""
    settings = e2e_settings(e2e_database_url).model_copy(update={"alerts_enabled": True})
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


def chains(bot: E2EBot) -> dict[str, FakeChain]:
    """A node per chain: Aave debt at HF 1.43 and one v3 range on Base, nothing elsewhere."""
    nodes = {host: FakeChain(d) for host, d in zip(INFURA_HOSTS, DEPLOYMENTS, strict=True)}
    base = nodes["base-mainnet.infura.io"]
    base.aave[WALLET] = AaveAccount(Decimal("27699.05"), Decimal("15221.12"), Decimal("1.43"))
    base.v3[TOKEN_ID] = V3Position(WALLET, POOL, -199560, -195770, 10**15)
    base.ticks[POOL] = -197404
    for host, node in nodes.items():
        bot.web.script(host, node.handle)
    return nodes


async def save_wallet(bot: E2EBot, who: Any) -> None:
    person = PersonRef(PLATFORM, who.id)
    await bot.process.facts.remember(Viewer(person, frozenset()), FactKind.ETH_WALLET, WALLET)


async def alert_rows(bot: E2EBot) -> list[Any]:
    async with bot.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, kind, chain, address, address_source, threshold, language, "
                "state, token_id, pool_ref FROM position_alert ORDER BY id"
            )
        )
        return list(rows)


def prompt(turn: Turn) -> Sent:
    """The one message of a turn that carries buttons."""
    [sent] = [s for s in turn.sent if s.buttons]
    return sent


def quiet(bot: E2EBot, turn: Turn, calls_before: int) -> None:
    """Never the corpus, never a model: an alert request is neither a search nor a question."""
    assert not turn.searched, "an alert request must never search the corpus"
    assert len(bot.chat.calls) == calls_before, "an alert request must never call a model"


async def test_a_portuguese_request_is_confirmed_created_and_later_messaged(
    alerts_bot: E2EBot,
) -> None:
    bot = alerts_bot
    nodes = chains(bot)
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    dm = bot.dm(leo)
    calls = len(bot.chat.calls)

    asked = await dm.say("me avisa se o health factor cair abaixo de 1.3")

    quiet(bot, asked, calls)
    offer = prompt(asked)
    assert offer.via == "reply"
    assert [label for label, _ in offer.buttons] == ["Confirmar", "Cancelar"]
    assert "**Base** — health factor no Aave abaixo de **1,30** · agora **1,43**" in offer.content
    assert WALLET in offer.content, "a direct message may name the saved wallet"
    assert "Ethereum" not in offer.content, "a chain with no debt is not offered"
    asked.assert_language("pt")
    assert await alert_rows(bot) == [], "nothing is stored before Confirm"

    confirmed = await dm.press(offer, "Confirmar")

    quiet(bot, confirmed, calls)
    [done] = confirmed.sent
    assert done.via == "response" and done.content.startswith("Pronto. Estou acompanhando:")
    assert done.disabled == {"Confirmar", "Cancelar"}
    confirmed.assert_language("pt")
    [row] = await alert_rows(bot)
    assert (row.kind, row.chain, row.address, row.address_source) == (
        "aave_health", "base", WALLET, "saved"
    )
    assert (row.threshold, row.language, row.state) == (Decimal("1.3"), "pt", "ok")
    assert f"**{row.id}** — health factor no Aave na Base abaixo de 1,30" in done.content

    clock = cast(FakeClock, bot.process.edges.clock)
    runner = bot.process.graph.alerts
    assert runner is not None
    nodes["base-mainnet.infura.io"].aave[WALLET].health_factor = Decimal("1.28")
    clock.advance(SWEEP)
    swept = await bot.turn(lambda: runner.run_due(clock()))
    [message] = swept.sent
    assert message.via == "dm"
    assert "caiu para **1,28** (seu limite 1,30)" in message.content
    swept.assert_language("pt")


async def test_cancel_creates_nothing_and_nobody_else_can_press(alerts_bot: E2EBot) -> None:
    bot = alerts_bot
    chains(bot)
    leo, ana = bot.person("Leo"), bot.person("Ana")
    await save_wallet(bot, leo)
    here = bot.channel("general", leo)
    calls = len(bot.chat.calls)

    asked = await here.say("alert me if my health factor drops below 1.25 on base")

    quiet(bot, asked, calls)
    offer = prompt(asked)
    assert offer.via == "reply" and not offer.ephemeral
    assert WALLET not in offer.content.lower(), "a channel never sees the wallet"
    assert "Aave health factor below **1.25** · now **1.43**" in offer.content

    intruder = await Conversation(bot, ana, here.channel).press(offer, "Confirm")
    [refusal] = intruder.sent
    assert refusal.ephemeral and refusal.content == "Only the person who asked can confirm this."
    assert await alert_rows(bot) == []

    cancelled = await here.press(offer, "Cancel")
    [note] = cancelled.sent
    assert note.content == "Cancelled. Nothing was created."
    assert note.disabled == {"Confirm", "Cancel"}
    assert (await here.press(offer, "Confirm")).sent == (), "a settled prompt takes no press"
    assert await alert_rows(bot) == []


async def test_list_and_delete_from_a_dm_and_ask_from_a_slash_command(
    alerts_bot: E2EBot,
) -> None:
    bot = alerts_bot
    chains(bot)
    leo, ana = bot.person("Leo"), bot.person("Ana")
    await save_wallet(bot, leo)
    calls = len(bot.chat.calls)

    asked = await bot.channel("general", leo).slash(
        "ask", question="tell me when my LP goes out of range"
    )

    quiet(bot, asked, calls)
    offer = prompt(asked)
    assert offer.via == "followup" and offer.ephemeral
    kinds = [kind for kind, _ in bot.discord.webhooks.events]
    assert kinds[-3:] == ["response", "delete", "followup"], "the public 'thinking' is removed"
    assert "Uniswap v3 WETH/USDC 0.05% #4558452, range" in offer.content
    assert "now in range" in offer.content
    assert "Positions you open later aren't covered" in offer.content
    await bot.channel("general", leo).press(offer, "Confirm")
    [row] = await alert_rows(bot)
    assert (row.kind, row.token_id, row.pool_ref) == ("lp_range", TOKEN_ID, POOL)
    assert row.state == "in_range"

    dm = bot.dm(leo)
    listed = await dm.slash("alert list")
    [listing] = listed.sent
    assert listing.ephemeral
    assert f"**{row.id}** - Uniswap v3 WETH/USDC 0.05% #4558452 on Base - in range" in (
        listing.content
    )
    listed_pt = await dm.slash("alert list", locale="pt-BR")
    assert "**Seus alertas (1):**" in listed_pt.text

    not_hers = await bot.dm(ana).slash("alert delete", alert=row.id)
    no_such = await bot.dm(ana).slash("alert delete", alert=row.id + 1000)
    assert not_hers.text == no_such.text, "not yours and no such alert read the same"
    assert len(await alert_rows(bot)) == 1

    deleted = await dm.slash("alert delete", alert=row.id)
    assert deleted.text == "Deleted. I won't watch that any more."
    assert await alert_rows(bot) == []
    assert "You have no alerts." in (await dm.slash("alert list")).text


async def test_an_out_of_bounds_limit_is_refused_with_the_bounds(alerts_bot: E2EBot) -> None:
    bot = alerts_bot
    leo = bot.person("Leo")
    await save_wallet(bot, leo)

    refused = await bot.dm(leo).say("me avisa se o health factor cair abaixo de 0,9")

    assert not refused.searched and refused.hosts == frozenset()
    assert [s.buttons for s in refused.sent] == [()]
    assert "entre 1,05 e 5,00" in refused.text
    refused.assert_language("pt")


async def test_with_alerts_off_a_request_is_told_so_and_never_searched(bot: E2EBot) -> None:
    leo = bot.person("Leo")

    for said, expected in (
        ("tell me when my LP goes out of range", "Alerts aren't available"),
        ("avise quando minha posição sair da faixa", "Alertas não estão disponíveis"),
    ):
        turn = await bot.dm(leo).say(said)
        assert not turn.searched
        assert turn.hosts == frozenset()
        assert turn.text.startswith(expected)

    listed = await bot.dm(leo).slash("alert list")
    assert "isn't switched on" in listed.text


async def test_with_two_saved_wallets_the_request_asks_which_then_uses_the_one_named(
    alerts_bot: E2EBot,
) -> None:
    bot = alerts_bot
    chains(bot)
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    person = PersonRef(PLATFORM, leo.id)
    other = "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0"
    await bot.process.facts.remember(Viewer(person, frozenset()), FactKind.ETH_WALLET, other)
    dm = bot.dm(leo)
    calls = len(bot.chat.calls)

    asked = await dm.say("alert me if my health factor drops below 1.3")

    quiet(bot, asked, calls)
    [which] = asked.sent
    assert not which.buttons, "nothing is offered until the wallet is chosen"
    assert which.content.startswith("You have several wallets saved (`…75e0`, `…63d6`)")
    assert await alert_rows(bot) == []

    named = await dm.say("alert me if my health factor drops below 1.3 on …63d6")

    quiet(bot, named, calls)
    offer = prompt(named)
    assert WALLET in offer.content and other not in offer.content
    await dm.press(offer, "Confirm")
    [row] = await alert_rows(bot)
    assert (row.kind, row.address, row.address_source) == ("aave_health", WALLET, "saved")
