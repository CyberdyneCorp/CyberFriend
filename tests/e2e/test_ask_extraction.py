""""What was asked of me this week", from capture to a cited answer.

The path no other scenario reaches: channel chatter the bot is not addressed
in is captured by the ingest half, the extraction pass reads it through the
production prompt and parser, and the question is then answered by the bot
from the ask rows -- on real Postgres, with no similarity search.

Seeded around the harness clock (Tuesday 1 September 2026, 12:00 UTC): Bea
asks Leo something yesterday and something else twelve days ago, Lia asks
Leo something in #leadership, which Leo cannot read, and Bea says something
that is not an ask at all.
"""

from __future__ import annotations

from datetime import timedelta

from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.model import ASK_EXTRACTION
from tests.e2e.harness.process import GENERAL, GUILD_ID, LEAD, NOW

REVIEW = "@Leo can you review the rollback plan before friday?"
INVOICES = "@Leo could you send me the invoice numbers?"
MEMO = "@Leo can you draft the reorg memo?"
CHATTER = "the coffee machine is working again"


def ask(text: str) -> dict[str, object]:
    """One entry of the extraction model's JSON: a request to Leo."""
    return {
        "kind": "request",
        "text": text,
        "addressee": "Leo",
        "addressee_is_group": False,
        "confidence": 0.9,
    }


async def test_a_captured_ask_is_answered_with_a_citation_to_its_message(bot: E2EBot) -> None:
    leo, bea = bot.person("Leo"), bot.person("Bea")
    lia = bot.person("Lia", roles=[LEAD])
    bot.chat.script_asks(REVIEW, ask("review the rollback plan before Friday"))
    bot.chat.script_asks(INVOICES, ask("send the invoice numbers"))
    bot.chat.script_asks(MEMO, ask("draft the reorg memo"))

    await bot.chatter("general", bea, REVIEW, mentions=[leo], at=NOW - timedelta(days=1))
    await bot.chatter("general", bea, INVOICES, mentions=[leo], at=NOW - timedelta(days=12))
    await bot.chatter("leadership", lia, MEMO, mentions=[leo], at=NOW - timedelta(hours=2))
    await bot.chatter("general", bea, CHATTER, at=NOW - timedelta(hours=1))

    # One model call per addressed message; the chatter is not a candidate.
    assert await bot.extract_asks() == (ASK_EXTRACTION,) * 3

    turn = await bot.channel("general", leo).say("what was asked of me this week?")

    link = f"https://discord.com/channels/{GUILD_ID}/{GENERAL}/{bot.corpus[REVIEW]}"
    assert "review the rollback plan before Friday" in turn.text
    assert link in turn.text
    # Outside the week, and in a channel Leo cannot read.
    assert "invoice" not in turn.text
    assert "reorg" not in turn.text
    # Answered from the rows: no search, no model.
    assert not turn.searched
    assert turn.schemas == ()
