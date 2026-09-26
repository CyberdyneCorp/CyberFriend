"""The one-time tracing notice, through the process `main` assembles.

With tracing on, a person's first reply ends with the notice in their language
and later replies do not; it is recorded on their person row; an opted-out
person never gets it; the capabilities reply states the same; and with tracing
off nothing mentions recording at all.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.app.tracing_notice import NOTICE_VERSION
from chatmemory.domain.identity import PersonRef
from tests.e2e.conftest import COFFEE
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.test_trace_withdrawal import _admin_opt_out, traced

__all__ = ["traced"]

EN_NOTICE = (
    "**Note:** Your questions and my answers are recorded for up to 90 days, and "
    "CyberFriend admins can read them. Use `/privacy` to see or delete them."
)
PT_NOTICE = (
    "**Aviso:** Suas perguntas e minhas respostas ficam registradas por até 90 dias, "
    "e os administradores do CyberFriend podem lê-las. Use `/privacy` para ver ou "
    "apagar."
)


async def _notice_version(engine: AsyncEngine, platform_user_id: int) -> int | None:
    async with engine.connect() as conn:
        version = await conn.scalar(
            text(
                "SELECT p.tracing_notice_version FROM person p "
                "JOIN person_platform_id i ON i.person_id = p.id "
                "WHERE i.platform_user_id = :uid"
            ),
            {"uid": platform_user_id},
        )
    return None if version is None else int(version)


async def test_the_first_channel_reply_carries_it_once(
    traced: E2EBot, clean: AsyncEngine
) -> None:
    bot = traced
    bea = bot.person("Bea")
    general = bot.channel("general", bea)

    first = await general.say("when will the coffee maker be fixed?")
    second = await general.say("when will the coffee maker be fixed?")

    assert COFFEE in first.text
    assert first.text.endswith(EN_NOTICE)
    assert EN_NOTICE not in second.text
    assert await _notice_version(clean, bea.id) == NOTICE_VERSION


async def test_a_portuguese_dm_gets_it_in_portuguese(traced: E2EBot) -> None:
    bot = traced
    turn = await bot.dm(bot.person("Bea")).say("quando a cafeteira vai ser consertada?")
    assert turn.text.endswith(PT_NOTICE)


async def test_an_opted_out_person_never_gets_it(
    traced: E2EBot, e2e_database_url: str, clean: AsyncEngine
) -> None:
    bot = traced
    bea = bot.person("Bea")
    await _admin_opt_out(e2e_database_url, PersonRef("discord", bea.id))

    turn = await bot.dm(bea).say("what can you do?")

    assert EN_NOTICE not in turn.text
    assert await _notice_version(clean, bea.id) is None


async def test_the_capabilities_reply_states_the_recording(traced: E2EBot) -> None:
    bot = traced
    turn = await bot.dm(bot.person("Bea")).say("what can you do?")
    assert "**Privacy**" in turn.text
    assert "recorded for up to 90 days" in turn.text


async def test_without_tracing_nothing_mentions_recording(bot: E2EBot) -> None:
    first = await bot.dm(bot.person("Bea")).say("what can you do?")
    assert "recorded" not in first.text
    assert "**Note:**" not in first.text
