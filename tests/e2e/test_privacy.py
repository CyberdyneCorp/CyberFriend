"""`/privacy` through the assembled process and real Postgres.

In a server channel the reply is private, shows counts and fact kinds and
never a value, and offers [Send me the details], which only the asker can
press and which sends the values by direct message. In a direct message the
values are shown straight away. Archive coverage names only channels the
person can read now: a channel they lost access to is neither named nor
counted. The retention statements follow the settings, never fixed text.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.e2e.harness.conversation import Conversation, E2EBot
from tests.e2e.harness.process import GENERAL, LEAD, LEADERSHIP, NOW, e2e_settings, start
from tests.e2e.harness.web import NetworkSeal

EMAIL = "leo.privacy@example.com"
PHONE = "+5521980703795"


async def _leo_with_facts(bot: E2EBot) -> Conversation:
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.say(f"my email is {EMAIL}")
    await dm.say(f"my phone number is {PHONE}")
    assert dict(await bot.fact_rows(leo)) == {"email": EMAIL, "phone": PHONE}
    return dm


async def test_in_a_channel_counts_and_kinds_only_then_details_by_dm(bot: E2EBot) -> None:
    dm = await _leo_with_facts(bot)
    leo = dm.who
    await bot.chatter("general", leo, "shipping the release tonight", at=NOW - timedelta(hours=1))
    here = bot.channel("general", leo)

    shown = await here.slash("privacy")

    assert not shown.searched and shown.schemas == ()
    assert shown.sent and all(s.via == "followup" and s.ephemeral for s in shown.sent)
    first = shown.sent[0]
    assert "Only you can see this" in first.content
    assert [label for label, _ in first.buttons] == ["Send me the details"]
    assert "Saved: email address, phone number." in shown.text
    assert f"<#{GENERAL}>: 1 message" in shown.text.splitlines()
    assert "No database backups are kept" in shown.text
    assert "I don't record your questions and answers" in shown.text
    for value in (EMAIL, PHONE, "980703795"):
        assert value not in shown.text, f"{value} shown in a server channel"

    ana = Conversation(bot, bot.person("Ana"), here.channel)
    intruder = await ana.press(first, "Send me the details")
    [refusal] = intruder.sent
    assert refusal.ephemeral and "Only the person who asked" in refusal.content
    assert not any(s.via == "dm" for s in intruder.sent)

    pressed = await here.press(first, "Send me the details")

    direct = [s for s in pressed.sent if s.via == "dm"]
    assert direct and all(s.channel_id == bot.discord.dm_channel_id(leo.id) for s in direct)
    details = "\n".join(s.text for s in direct)
    assert f"`{EMAIL}`" in details and PHONE in details
    assert "Kept even when your data is deleted" in details
    [note] = [s for s in pressed.sent if s.via == "followup"]
    assert note.ephemeral and "Sent the details to your direct messages" in note.content
    [redrawn] = [s for s in pressed.sent if s.via == "edit"]
    assert redrawn.disabled == {"Send me the details"}


async def test_in_a_dm_the_values_are_shown(bot: E2EBot) -> None:
    dm = await _leo_with_facts(bot)

    shown = await dm.slash("privacy")

    assert all(s.ephemeral for s in shown.sent)
    assert shown.sent[0].content == "Here's everything I hold about you."
    assert f"`{EMAIL}`" in shown.text and PHONE in shown.text
    assert not any(s.buttons for s in shown.sent)


async def test_a_channel_the_person_can_no_longer_read_is_neither_named_nor_counted(
    bot: E2EBot,
) -> None:
    lia = bot.person("Lia", roles=[LEAD])
    await bot.chatter("general", lia, "lunch at noon?", at=NOW - timedelta(hours=3))
    await bot.chatter("leadership", lia, "budget draft is ready", at=NOW - timedelta(hours=2))
    await bot.chatter("leadership", lia, "reviewing it tomorrow", at=NOW - timedelta(hours=1))
    lia = bot.discord.set_roles(lia, [])

    shown = await bot.dm(lia).slash("privacy")

    assert f"<#{GENERAL}>: 1 message" in shown.text.splitlines()
    assert str(LEADERSHIP) not in shown.text
    assert "leadership" not in shown.text
    assert "2 messages" not in shown.text and "3 messages" not in shown.text


async def test_forgetting_everything_points_to_privacy(bot: E2EBot) -> None:
    dm = await _leo_with_facts(bot)

    forgot = await dm.say("forget everything you know about me")

    assert "`/privacy` shows everything else I hold about you" in forgot.text
    assert await bot.fact_rows(dm.who) == []


async def test_portuguese_caller_gets_portuguese_statements(bot: E2EBot) -> None:
    shown = await bot.dm(bot.person("Bia")).slash("privacy", locale="pt-BR")

    assert "Mantido mesmo quando seus dados são apagados" in shown.text
    assert "Não são mantidos backups" in shown.text
    shown.assert_language("pt")


# --- statements follow the settings ------------------------------------------------

LANGFUSE = "langfuse.e2e.test"


@pytest_asyncio.fixture
async def configured(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """Tracing on with a 30-day retention, and backups kept 14 days."""
    settings = e2e_settings(e2e_database_url).model_copy(
        update={
            "tracing_enabled": True,
            "langfuse_host": f"https://{LANGFUSE}",
            "langfuse_public_key": SecretStr("pk-e2e"),
            "langfuse_secret_key": SecretStr("sk-e2e"),
            "trace_retention_days": 30,
            "backup_retention_days": 14,
        }
    )
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


async def test_the_statements_are_the_configured_periods(configured: E2EBot) -> None:
    shown = await configured.dm(configured.person("Leo")).slash("privacy")

    assert "recorded for up to 30 days, and admins can read them" in shown.text
    assert "Recorded now: 0 of your questions." in shown.text
    assert "Database backups, until they age out after 14 days." in shown.text
    assert "90 days" not in shown.text
