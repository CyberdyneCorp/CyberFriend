"""S: "o que você pode fazer?" lists what this deployment runs, and nothing else.

Through the whole process: settings -> registered tools -> the answer the
Discord wire receives. A deployment with wallet tools, alerts and voice on is
told about crypto, alerts, decisions and voice in Portuguese; the scheduled
questions and market data it does not run are not mentioned.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.formatting import DISCORD_MESSAGE_LIMIT
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.process import e2e_settings, start
from tests.e2e.harness.web import NetworkSeal


@pytest_asyncio.fixture
async def full_bot(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """Wallet tools (the harness default), alerts and voice on; schedules and
    market data left off."""
    settings = e2e_settings(e2e_database_url).model_copy(
        update={
            "alerts_enabled": True,
            "voice_questions_enabled": True,
            "media_api_key": SecretStr("e2e-media"),
        }
    )
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


async def test_a_portuguese_dm_is_told_everything_that_is_on(full_bot: E2EBot) -> None:
    turn = await full_bot.dm(full_bot.person("Ana")).say("o que você pode fazer?")

    assert not turn.searched
    assert turn.edge() == "NONE"
    assert all(len(sent.content) <= DISCORD_MESSAGE_LIMIT for sent in turn.sent)
    text = turn.text
    # Decisions and the other conversation routes.
    assert "o que decidimos sobre o deploy?" in text
    assert "o que eu perdi no #general?" in text
    # Crypto, from the registered wallet tools.
    assert "**Cripto**" in text
    assert "quanto eu tenho no total?" in text
    assert "o que minha carteira fez essa semana?" in text
    # Alerts, asked in words and confirmed.
    assert "**Alertas**" in text
    assert "me avisa quando meu LP ficar fora do range" in text
    assert "`/alert list`" in text
    # Voice, in a DM.
    assert "me mande um áudio" in text
    # What this deployment does not run.
    assert "/schedule" not in text
    assert "**Mercado**" not in text
    assert "Bitcoin" not in text
    turn.assert_language("pt")


async def test_a_switched_off_feature_is_not_mentioned(bot: E2EBot) -> None:
    """The default harness deployment: no alerts, no voice."""
    turn = await bot.dm(bot.person("Ana")).say("o que você pode fazer?")

    assert "**Cripto**" in turn.text
    assert "**Alertas**" not in turn.text
    assert "/alert" not in turn.text
    assert "áudio" not in turn.text
    turn.assert_language("pt")
