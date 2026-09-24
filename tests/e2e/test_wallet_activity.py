"""Wallet activity, end to end: "o que minha carteira fez essa semana?" read from the explorer.

The whole process runs: routing, the tool proposal, the egress guard admitting
the saved wallet, the real positions provider reading Blockscout's advanced
filters and the fake Base node for Aave's tokens and prices, and the reply on
the wire. Base's explorer serves a real week reduced (`activity_rows`): an
EIP-7702 wallet whose Aave supply and LP withdrawal were relayed, so neither
is in the address's transaction list -- and both must be in the answer.
"""

from __future__ import annotations

import re
from decimal import Decimal

import discord
import httpx

from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.discord.bot import PLATFORM
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.facts import FactKind
from tests.e2e.conftest import COLLEAGUE_CRYPTO
from tests.e2e.harness.chain import USDC, WETH, FakeChain
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.web import explorer
from tests.unit.activity_rows import LP_SENT_TX, RELAYER_FEES, WALLET, base_week

BASE_NODE = "base-mainnet.infura.io"
BASE_EXPLORER = "base.blockscout.com"
ARBITRUM_EXPLORER = "arbitrum.blockscout.com"
ETH_USD = Decimal("4000")
FULL_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def base_node() -> FakeChain:
    chain = FakeChain(DEPLOYMENTS[1])
    chain.prices.update({WETH: ETH_USD, USDC: Decimal("1")})
    return chain


def base_explorer(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v2/advanced-filters":
        return httpx.Response(200, json={"items": base_week(), "next_page_params": None})
    if path == f"/api/v2/addresses/{WALLET}/transactions":
        decoded = {
            "method_id": "ac9650d8",
            "parameters": [{"name": "data", "value": ["0x0c49ccbe", "0xfc6f7865"]}],
        }
        return httpx.Response(200, json={"items": [{"hash": LP_SENT_TX, "decoded_input": decoded}]})
    return explorer(request)


def script(bot: E2EBot) -> None:
    bot.web.script(BASE_NODE, base_node().handle)
    bot.web.script(BASE_EXPLORER, base_explorer)


async def save_wallet(bot: E2EBot, who: discord.Member) -> None:
    person = PersonRef(PLATFORM, who.id)
    await bot.process.facts.remember(Viewer(person, frozenset()), FactKind.ETH_WALLET, WALLET)


async def test_a_dm_with_a_saved_wallet_lists_the_relayed_week_per_chain(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    script(bot)

    turn = await bot.dm(leo).say("o que minha carteira fez essa semana?")

    assert turn.edge() == "CHAIN"
    assert not turn.searched, "a wallet's history must never search the corpus"
    assert turn.schemas == ("tools",), "one tool proposal, and no model writes the figures"
    text = turn.text
    assert "_Período: " in text
    assert "**Base** — 6 ações" in text
    assert "trocou 0,00399697 ETH → 10,2804 USDC" in text
    # The relayed actions the address list leaves out.
    assert "Aave: depositou 0,0222090 ETH (≈ US$ 88,84)" in text
    assert "Uniswap: saque de LP (liquidez e/ou taxas) — +10,5027 USDC" in text
    # The wallet's own multicall, named by its inner calls.
    assert "Uniswap: removeu liquidez — +48,367 USDC" in text
    assert f"enviou 0,009589 USDC (≈ US$ 0,01) para `{RELAYER_FEES}`" in text
    assert "Ocultos: 1 transferência(s) de token não solicitada(s)" in text
    assert "**Ethereum**, **Arbitrum** — nenhuma atividade" in text
    assert COLLEAGUE_CRYPTO not in text
    assert any(r.url.path == "/api/v2/advanced-filters" for r in bot.web.calls)
    turn.assert_language("pt")


async def test_the_same_question_in_a_channel_redacts_every_counterparty(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    script(bot)

    turn = await bot.channel("general", leo).say("what did my wallet do this week?")

    assert turn.edge() == "CHAIN" and not turn.searched
    assert "sent 0.009589 USDC (≈ $0.01) to an external address" in turn.text
    assert "Aave: supplied 0.0222090 ETH" in turn.text
    assert not FULL_ADDRESS.search(turn.text), "no full address is posted to a channel"


async def test_an_explorer_that_fails_is_said_and_never_read_as_no_activity(
    bot: E2EBot,
) -> None:
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    script(bot)
    bot.web.script(ARBITRUM_EXPLORER, lambda _: httpx.Response(503, text="upstream down"))

    turn = await bot.dm(leo).say("what did my wallet do this week?")

    assert turn.edge() == "CHAIN"
    assert "**Arbitrum** — could not be read (could not be reached)" in turn.text
    assert "**Ethereum** — no activity" in turn.text
