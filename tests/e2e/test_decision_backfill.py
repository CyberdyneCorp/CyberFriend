"""History captured before the decision log, answered after a backfill.

Ingest captured a proposal and its conclusion in #general, and the ask pass
read them when nothing extracted decisions: both are recorded as extracted,
no decision row exists, and the question falls through. The operator runs the
backfill, the backlog worker drains what it reset through the production
prompt, parser and store, and the same question gets a dated, cited answer.
Chatter with no decision marker is not read a second time.

Seeded around the harness clock (Tuesday 1 September 2026, 12:00 UTC).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.model import ASK_EXTRACTION
from tests.e2e.harness.process import GENERAL, GUILD_ID, NOW

PROPOSAL = "bora fazer o deploy na sexta?"
CONCLUSION = "fechou, deploy na sexta então"
CHATTER = "a máquina de café voltou a funcionar"
SUMMARY = "O deploy passa a ser na sexta"
DECIDED_AT = datetime(2026, 8, 20, 15, 0, tzinfo=NOW.tzinfo)


async def test_history_from_before_the_feature_yields_cited_decisions(bot: E2EBot) -> None:
    leo, bea = bot.person("Leo"), bot.person("Bea")
    await bot.chatter("general", bea, PROPOSAL, at=DECIDED_AT - timedelta(minutes=2))
    await bot.chatter("general", leo, CONCLUSION, at=DECIDED_AT)
    await bot.chatter("general", bea, CHATTER, at=DECIDED_AT + timedelta(minutes=5))
    # The passes as they ran before decisions: the live one and the backlog
    # (which also reads the harness's seeded corpus) read everything, and
    # nothing is decided.
    await bot.extract_asks()
    await bot.drain_backlog()
    assert await bot.drain_backlog() == ()
    assert await bot.decision_rows() == []

    bot.chat.script_decisions(
        CONCLUSION, {"summary": SUMMARY, "topic": "deploy", "confidence": 0.9}
    )
    report = await bot.ingest.backfill_decisions(date(2026, 8, 1))

    # The proposal and the conclusion carry a marker; the chatter does not.
    assert (report.matched, report.reset) == (2, 2)
    assert await bot.drain_backlog() == (ASK_EXTRACTION,) * 2
    assert [row.source_message_id for row in await bot.decision_rows()] == [
        bot.corpus[CONCLUSION]
    ]
    assert await bot.drain_backlog() == (), "drained once, not paid for again"

    turn = await bot.dm(bea).say("o que decidimos sobre o deploy?")

    assert turn.schemas == (), "the answer path calls no chat model"
    lines = turn.text.splitlines()
    assert lines[0] == "Decisões sobre o deploy:"
    assert lines[1] == f"1. 20/08/2026 — Leo: {SUMMARY} [1]"
    citation = f"https://discord.com/channels/{GUILD_ID}/{GENERAL}/{bot.corpus[CONCLUSION]}"
    assert citation in turn.text
    assert CONCLUSION in turn.text
