""""O que decidimos sobre o deploy?", from chatter to a dated, cited answer.

Both processes: ingest captures a proposal and the conclusion that settles it
in #general, the extraction pass stores the decision through the production
prompt, parser and store, and the bot answers the question from the row --
with no chat-model call, one topic embedding, the date in Sao Paulo, and a
citation that lands on the concluding message.

Seeded around the harness clock (Tuesday 1 September 2026, 12:00 UTC, 09:00 in
Sao Paulo). The conclusion is at 01:30 UTC on Saturday 29 August: still Friday
the 28th in Sao Paulo, which is the date the answer must show.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import discord
import pytest

from chatmemory.domain.identity import PersonRef
from tests.e2e.harness.conversation import PLATFORM, E2EBot
from tests.e2e.harness.model import ASK_EXTRACTION
from tests.e2e.harness.process import GENERAL, GUILD_ID, LEAD, LEADERSHIP, NOW

PROPOSAL = "bora fazer o deploy na sexta?"
CONCLUSION = "fechou, deploy na sexta então"
SUMMARY = "O deploy passa a ser na sexta"
DECIDED_AT = datetime(2026, 8, 29, 1, 30, tzinfo=NOW.tzinfo)

FREEZE = "fechado: congelamos o deploy até outubro"
FREEZE_SUMMARY = "O deploy fica congelado até outubro"

COFFEE = "a máquina de café será trocada na sexta"


def link(bot: E2EBot, content: str, channel: int = GENERAL) -> str:
    return f"https://discord.com/channels/{GUILD_ID}/{channel}/{bot.corpus[content]}"


async def settle_the_deploy(bot: E2EBot) -> dict[str, discord.Member]:
    """Bea proposes, Leo concludes, in #general; the pass stores one decision."""
    people = {"leo": bot.person("Leo"), "bea": bot.person("Bea")}
    bot.chat.script_decisions(
        CONCLUSION, {"summary": SUMMARY, "topic": "deploy", "confidence": 0.9}
    )
    await bot.chatter("general", people["bea"], PROPOSAL, at=DECIDED_AT - timedelta(minutes=2))
    await bot.chatter("general", people["leo"], CONCLUSION, at=DECIDED_AT)
    assert await bot.extract_asks() == (ASK_EXTRACTION,) * 2
    assert len(await bot.decision_rows()) == 1
    return people


async def test_a_portuguese_question_gets_a_dated_cited_portuguese_answer(bot: E2EBot) -> None:
    people = await settle_the_deploy(bot)

    turn = await bot.dm(people["bea"]).say("o que decidimos sobre o deploy?")

    assert turn.schemas == (), "the answer path calls no chat model"
    lines = turn.text.splitlines()
    assert lines[0] == "Decisões sobre o deploy:"
    assert lines[1] == f"1. 28/08/2026 — Leo: {SUMMARY} [1]"
    assert link(bot, CONCLUSION) in turn.text, "the citation lands on the conclusion"
    assert CONCLUSION in turn.text, "the citation quotes the original words"
    # Not `turn.assert_language("pt")`: the citation block's "From this
    # server" heading is the surface's, and English for every cited answer.


async def test_an_english_question_gets_an_english_answer(bot: E2EBot) -> None:
    people = await settle_the_deploy(bot)

    turn = await bot.dm(people["bea"]).say("what did we decide about the deploy last week?")

    assert turn.schemas == ()
    lines = turn.text.splitlines()
    assert lines[0] == "Decisions about the deploy from 24 Aug to 30 Aug:"
    # The summary stays in the language the decision was taken in.
    assert lines[1] == f"1. 28 Aug 2026 — Leo: {SUMMARY} [1]"
    assert link(bot, CONCLUSION) in turn.text


async def test_a_leadership_decision_asked_about_in_general_falls_through_unleaked(
    bot: E2EBot,
) -> None:
    """Lia may read #leadership; #general may not, so neither may the answer."""
    lia = bot.person("Lia", roles=[LEAD])
    # Somebody in #general who cannot read #leadership, or the room's
    # audience would be Lia alone and could read everything she can.
    bot.person("Bea")
    bot.chat.script_decisions(
        FREEZE, {"summary": FREEZE_SUMMARY, "topic": "deploy", "confidence": 0.9}
    )
    await bot.chatter("leadership", lia, FREEZE, at=NOW - timedelta(hours=2))
    assert await bot.extract_asks() == (ASK_EXTRACTION,)

    public = await bot.channel("general", lia).say("o que decidimos sobre o deploy?")

    assert "Decisões sobre" not in public.text
    for secret in (FREEZE_SUMMARY, FREEZE, f"/{LEADERSHIP}/"):
        assert secret not in public.text, f"{secret!r} reached #general"
    assert public.searched, "fell through to ordinary retrieval"

    private = await bot.dm(lia).say("o que decidimos sobre o deploy?")
    assert FREEZE_SUMMARY in private.text
    assert link(bot, FREEZE, LEADERSHIP) in private.text


@pytest.mark.parametrize("deleted", [CONCLUSION, PROPOSAL])
async def test_deleting_the_source_or_the_proposal_takes_the_decision_away(
    bot: E2EBot, deleted: str
) -> None:
    people = await settle_the_deploy(bot)

    await bot.delete("general", deleted)

    turn = await bot.dm(people["bea"]).say("o que decidimos sobre o deploy?")
    assert SUMMARY not in turn.text
    assert "Decisões sobre" not in turn.text
    assert turn.searched, "fell through to ordinary retrieval"
    assert await bot.decision_rows() == []


async def test_no_matching_decision_falls_through_to_retrieval(bot: E2EBot) -> None:
    people = await settle_the_deploy(bot)
    bea = people["bea"]
    await bot.seed_conversation(
        "general", [(PersonRef(PLATFORM, bea.id), "Bea", COFFEE, NOW - timedelta(days=1))]
    )

    turn = await bot.dm(bea).say("o que decidimos sobre a máquina de café?")

    assert "Decisões sobre" not in turn.text
    assert SUMMARY not in turn.text, "the deploy decision is not near enough to list"
    assert turn.edge() == "CORPUS"
    assert COFFEE in turn.text, "retrieval answered from the conversation"
    assert "grounded_answer" in turn.schemas
