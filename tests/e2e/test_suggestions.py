"""`/suggest` and `/suggestions` through the assembled process and real Postgres.

What a person meets: a private acknowledgement with the suggestion's number
and who will see it, [Yes] [No] that only they can press, their own list back,
and refusals that store nothing. Rows are read back by SQL.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from chatmemory.adapters.discord.bot import PLATFORM
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import PersonRef
from tests.e2e.harness.conversation import Conversation, E2EBot
from tests.e2e.harness.process import GENERAL, GUILD_ID


async def suggestion_rows(bot: E2EBot) -> list[Any]:
    async with bot.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, text, language, source_kind, guild_id, channel_id, status, "
                "notify_on_change FROM feature_request ORDER BY id"
            )
        )
        return list(rows)


async def test_suggest_then_list_it_and_opt_into_status_news(bot: E2EBot) -> None:
    leo, ana = bot.person("Leo"), bot.person("Ana")
    here = bot.channel("general", leo)

    suggested = await here.slash("suggest", text="Show a weekly digest of decisions")

    assert not suggested.searched and suggested.schemas == ()
    [ack] = suggested.sent
    assert ack.via == "followup" and ack.ephemeral
    [row] = await suggestion_rows(bot)
    assert ack.content.startswith(f"Recorded as **#{row.id}**.")
    assert "The team will see your text and your Discord name" in ack.content
    assert [label for label, _ in ack.buttons] == ["Yes", "No"]
    assert (row.text, row.language, row.source_kind) == (
        "Show a weekly digest of decisions",
        "en",
        "command",
    )
    assert (row.guild_id, row.channel_id, row.status) == (GUILD_ID, GENERAL, "new")
    assert row.notify_on_change is False, "no messages until they say yes"

    intruder = await Conversation(bot, ana, here.channel).press(ack, "Yes")
    [refusal] = intruder.sent
    assert refusal.ephemeral and "Only the person who made this suggestion" in refusal.content
    assert (await suggestion_rows(bot))[0].notify_on_change is False

    answered = await here.press(ack, "Yes")
    [note] = answered.sent
    assert "I'll send you a direct message when its status changes" in note.content
    assert note.disabled == {"Yes", "No"}
    assert (await suggestion_rows(bot))[0].notify_on_change is True

    listed = await bot.dm(leo).slash("suggestions")
    [listing] = listed.sent
    assert listing.ephemeral
    assert f"**#{row.id}** - new - Show a weekly digest of decisions" in listing.content
    assert "Show a weekly digest" not in (await bot.dm(ana).slash("suggestions")).text


async def test_the_same_suggestion_twice_is_one_record(bot: E2EBot) -> None:
    dm = bot.dm(bot.person("Leo"))
    await dm.slash("suggest", text="Dark mode, please!")

    again = await dm.slash("suggest", text="dark mode please")

    [row] = await suggestion_rows(bot)
    assert again.text == f"You already suggested that: it's recorded as **#{row.id}**."
    assert (row.guild_id, row.channel_id) == (None, None), "a DM keeps no location"


async def test_contact_details_are_refused_with_the_reason(bot: E2EBot) -> None:
    dm = bot.dm(bot.person("Leo"))

    refused = await dm.slash("suggest", text="email me at leo@example.com when it ships")

    assert "it contains an email address" in refused.text
    assert refused.sent[0].buttons == ()
    assert await suggestion_rows(bot) == []


async def test_answered_in_portuguese(bot: E2EBot) -> None:
    dm = bot.dm(bot.person("Leo"))

    suggested = await dm.slash(
        "suggest", locale="pt-BR", text="avisar quando alguém me marcar numa mensagem"
    )

    suggested.assert_language("pt")
    assert "A equipe vai ver o seu texto" in suggested.text
    assert [label for label, _ in suggested.sent[0].buttons] == ["Sim", "Não"]
    [row] = await suggestion_rows(bot)
    assert row.language == "pt"


async def test_opting_out_deletes_suggestions_and_takes_no_more(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.slash("suggest", text="Dark mode")
    await bot.dm(bot.person("Ana")).slash("suggest", text="Light mode")

    await OptOutService(PostgresRetentionStore(bot.engine)).opt_out(
        PersonRef(PLATFORM, leo.id), "e2e"
    )

    assert [r.text for r in await suggestion_rows(bot)] == ["Light mode"]
    refused = await dm.slash("suggest", text="Sepia mode")
    assert "You've opted out" in refused.text
    assert [r.text for r in await suggestion_rows(bot)] == ["Light mode"]
