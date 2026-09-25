"""`/suggest` and `/suggestions` through the assembled process and real Postgres.

A suggestion written as a message is proposed with [Record suggestion] and
[No, answer it], stored only on the first, and otherwise answered as the
message would have been; every other route wins over proposing it.

What a person meets with the command: a private acknowledgement with the suggestion's number
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
from tests.e2e.harness.conversation import Conversation, E2EBot, Turn
from tests.e2e.harness.discord_wire import Sent
from tests.e2e.harness.process import GENERAL, GUILD_ID
from tests.e2e.test_alert_kinds import COINGECKO, Market, alerts_bot  # noqa: F401
from tests.e2e.test_portfolio import BASE_HOST, base_chain, save_wallet
from tests.e2e.test_preferred_currency import bitcoin, market_bot  # noqa: F401


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


# --- suggestions written as messages -------------------------------------------------


def proposal(turn: Turn) -> Sent:
    [sent] = turn.sent
    assert [label for label, _ in sent.buttons] == ["Record suggestion", "No, answer it"]
    return sent


async def test_a_suggestion_in_a_message_is_recorded_only_when_confirmed(bot: E2EBot) -> None:
    leo, ana = bot.person("Leo"), bot.person("Ana")
    here = bot.channel("general", leo)

    asked = await here.say("I have a feature request: tell me when someone mentions me")

    offer = proposal(asked)
    assert not asked.searched and asked.schemas == (), "a proposal searches and asks nothing"
    assert offer.via == "reply"
    assert "> tell me when someone mentions me" in offer.content
    assert "They will see your text and your name" in offer.content
    assert await suggestion_rows(bot) == [], "nothing is stored before the press"

    intruder = await Conversation(bot, ana, here.channel).press(offer, "Record suggestion")
    [refusal] = intruder.sent
    assert refusal.ephemeral and "Only the person who made this suggestion" in refusal.content
    assert await suggestion_rows(bot) == []

    recorded = await here.press(offer, "Record suggestion")
    [ack] = recorded.sent
    [row] = await suggestion_rows(bot)
    assert ack.content.startswith(f"Recorded as **#{row.id}**.")
    assert [label for label, _ in ack.buttons] == ["Yes", "No"]
    assert (row.text, row.language, row.source_kind) == (
        "tell me when someone mentions me",
        "en",
        "channel",
    )
    assert (row.guild_id, row.channel_id, row.notify_on_change) == (GUILD_ID, GENERAL, False)
    assert not recorded.searched

    answered = await here.press(ack, "Yes")
    assert "I'll send you a direct message" in answered.text
    assert (await suggestion_rows(bot))[0].notify_on_change is True


async def test_a_portuguese_suggestion_is_proposed_in_portuguese(bot: E2EBot) -> None:
    dm = bot.dm(bot.person("Leo"))

    asked = await dm.say("tenho uma sugestão: avisar quando alguém me marcar")

    [offer] = asked.sent
    assert [label for label, _ in offer.buttons] == ["Registrar sugestão", "Não, responda"]
    assert "> avisar quando alguém me marcar" in offer.content
    asked.assert_language("pt")

    recorded = await dm.press(offer, "Registrar sugestão")
    [row] = await suggestion_rows(bot)
    assert recorded.text.startswith(f"Registrada como **#{row.id}**.")
    assert (row.text, row.language, row.source_kind) == (
        "avisar quando alguém me marcar",
        "pt",
        "dm",
    )
    assert (row.guild_id, row.channel_id) == (None, None)


async def test_declining_answers_the_message_and_stores_nothing(bot: E2EBot) -> None:
    dm = bot.dm(bot.person("Leo"))
    offer = proposal(await dm.say("it would be nice if you could summarise the launch thread"))

    declined = await dm.press(offer, "No, answer it")

    note, *answer = declined.sent
    assert note.via == "response" and note.content == "Not recorded. Here's the answer instead."
    assert note.disabled == {"Record suggestion", "No, answer it"}
    assert declined.searched, "answered as the message would have been without the proposal"
    assert answer and answer[0].via == "reply"
    assert await suggestion_rows(bot) == []
    assert (await dm.press(offer, "Record suggestion")).sent == (), "one answer per proposal"


async def test_an_unanswered_proposal_answers_the_message_when_it_expires(bot: E2EBot) -> None:
    dm = bot.dm(bot.person("Leo"))
    offer = proposal(await dm.say("feature request: a weekly digest of decisions"))

    expired = await dm.expire(offer)

    note, *answer = expired.sent
    assert note.via == "edit"
    assert note.content == "No answer, so I didn't record it. Here's the answer instead."
    assert note.disabled == {"Record suggestion", "No, answer it"}
    assert expired.searched and answer
    assert await suggestion_rows(bot) == []


async def test_words_about_suggestions_are_questions(bot: E2EBot) -> None:
    dm = bot.dm(bot.person("Leo"))

    for question in ("qual foi a sugestão do João?", "you should be able to tell me X"):
        turn = await dm.say(question)
        assert all(sent.buttons == () for sent in turn.sent), question
        assert turn.searched, question


async def test_a_price_question_after_the_form_is_answered_with_the_price(
    market_bot: E2EBot,  # noqa: F811 - the fixture imported above
) -> None:
    bot = market_bot
    bot.web.script(COINGECKO, bitcoin)

    turn = await bot.dm(bot.person("Leo")).say("sugestão: me diga o preço do BTC")

    assert turn.edge() == "MARKET" and not turn.searched
    assert "Bitcoin (BTC): 63,210 USD" in turn.text
    assert all(sent.buttons == () for sent in turn.sent)
    assert await suggestion_rows(bot) == []


async def test_a_portfolio_request_after_the_form_is_answered_from_the_chain(
    bot: E2EBot,
) -> None:
    leo = bot.person("Leo")
    await save_wallet(bot, leo)
    bot.web.script(BASE_HOST, base_chain().handle)

    turn = await bot.dm(leo).say("it would be nice if you could show my portfolio")

    assert turn.edge() == "CHAIN" and not turn.searched
    assert "Total ≈" in turn.text
    assert all(sent.buttons == () for sent in turn.sent)


async def test_an_alert_request_after_the_form_is_an_alert_proposal(
    alerts_bot: E2EBot,  # noqa: F811 - the fixture imported above
) -> None:
    bot = alerts_bot
    bot.web.script(COINGECKO, Market(btc="97412.35").handle)
    leo = bot.person("Leo")
    await save_wallet(bot, leo)

    turn = await bot.dm(leo).say("feature request: notify me when BTC hits 100k")

    [offer] = turn.sent
    assert [label for label, _ in offer.buttons] == ["Confirm", "Cancel"]
    assert "**BTC** above **$100,000**" in offer.content
    assert await suggestion_rows(bot) == []
