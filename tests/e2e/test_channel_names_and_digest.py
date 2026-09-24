"""A channel typed by name works like one picked from Discord's list.

From production. "resuma o que aconteceu no #general" scheduled with
`/schedule create`, and "Oque eu perdi no #general ?" in a DM, both arrived as
text: Discord makes a channel link only when the channel is picked from its
list, and cannot make one in a DM at all. Catch-up refused ("Tell me which
channel to catch up on..."), every day, as a scheduled DM. The /schedule
confirmation was also English for a Portuguese question.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.app.catchup import NAME_THE_CHANNEL
from chatmemory.app.localise import PORTUGUESE
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.process import GENERAL, e2e_settings, start
from tests.e2e.harness.web import NetworkSeal


@pytest_asyncio.fixture
async def schedule_bot(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """The production process as it runs with `SCHEDULED_TASKS_ENABLED=true`."""
    settings = e2e_settings(e2e_database_url).model_copy(
        update={"scheduled_tasks_enabled": True}
    )
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


async def test_a_typed_channel_name_in_a_dm_is_caught_up_on(bot: E2EBot) -> None:
    turn = await bot.dm(bot.person("Ana")).say("Oque eu perdi no #general nos últimos 3 dias?")

    assert NAME_THE_CHANNEL not in turn.text
    assert PORTUGUESE[NAME_THE_CHANNEL] not in turn.text
    assert turn.searched, "catch-up did not search the channel"


async def test_an_unknown_channel_name_is_still_refused(bot: E2EBot) -> None:
    turn = await bot.dm(bot.person("Ana")).say("o que eu perdi no #inexistente?")
    assert turn.text.strip() == PORTUGUESE[NAME_THE_CHANNEL].strip()


async def test_a_scheduled_question_is_stored_with_the_channel_linked(
    schedule_bot: E2EBot,
) -> None:
    bot = schedule_bot
    ana = bot.person("Ana")
    await bot.dm(ana).say("oi")  # the bot keeps nothing for someone it never talked to
    turn = await bot.dm(ana).slash(
        "schedule create", question="resuma o que aconteceu no #general", every_hours=24
    )

    assert turn.text.startswith("Pronto."), turn.text
    async with bot.engine.connect() as conn:
        stored: str = (
            await conn.execute(text("SELECT question FROM scheduled_task"))
        ).scalar_one()
    assert stored == f"resuma o que aconteceu no <#{GENERAL}>"


async def test_schedule_replies_follow_the_client_language(schedule_bot: E2EBot) -> None:
    bot = schedule_bot
    turn = await bot.dm(bot.person("Ana")).slash("schedule list", locale="pt-BR")
    assert turn.text.startswith("Você não tem perguntas agendadas."), turn.text


async def test_a_stranger_is_told_to_talk_first_not_that_they_hit_the_cap(
    schedule_bot: E2EBot,
) -> None:
    """Regression: UNKNOWN_PERSON fell through to "you've reached the number of
    scheduled questions I can keep", said to somebody with none."""
    turn = await schedule_bot.dm(schedule_bot.person("Ana")).slash(
        "schedule create", question="resuma o que aconteceu no #general", every_hours=24
    )
    assert turn.text.startswith("Preciso ter conversado com você"), turn.text
