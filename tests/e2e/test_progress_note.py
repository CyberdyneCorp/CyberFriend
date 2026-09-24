"""A slow answer says it is still coming, and the note goes when the answer arrives.

From production: a portfolio over three chains took 30-90 s on the deployed
Infura key with only the typing indicator showing, which reads as a broken bot.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from chatmemory.adapters.discord import bot as discord_bot
from chatmemory.adapters.discord.bot import PROGRESS
from chatmemory.app.language import Language
from tests.e2e.harness.conversation import E2EBot

QUESTION = "o que aconteceu com o foguete da equipe de marte?"


def _slow(bot: E2EBot, seconds: float) -> None:
    asks = bot.discord.client._asks
    original = asks.ask

    async def ask(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(seconds)
        return await original(*args, **kwargs)

    asks.ask = ask  # type: ignore[method-assign]


async def test_a_slow_answer_shows_a_note_in_the_askers_language_then_removes_it(
    bot: E2EBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discord_bot, "PROGRESS_AFTER_SECONDS", 0.01)
    _slow(bot, 0.05)

    turn = await bot.dm(bot.person("Ana")).say(QUESTION)

    notes = [s for s in turn.sent if s.content == PROGRESS[Language.PORTUGUESE]]
    assert len(notes) == 1, [s.content for s in turn.sent]
    assert notes[0].message_id in bot.discord.http.deleted
    # The answer itself still arrives, after the note.
    assert turn.sent[-1].content != PROGRESS[Language.PORTUGUESE]
    assert turn.sent.index(notes[0]) < len(turn.sent) - 1


async def test_a_fast_answer_shows_no_note(bot: E2EBot) -> None:
    turn = await bot.dm(bot.person("Ana")).say(QUESTION)

    assert all(s.content not in PROGRESS.values() for s in turn.sent)
    assert bot.discord.http.deleted == []
