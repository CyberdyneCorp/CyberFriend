""""Fechou, vamos com X": a decision captured in chatter becomes a stored row.

The ingest half only, since nothing reads decisions yet: a Portuguese
conclusion nobody was addressed in is captured, passes the candidate filter on
its decision marker alone, is read by the extraction pass through the
production prompt and parser, and is stored against the message that settled
it -- with the proposal it settled recorded as evidence. The proposal itself,
which carries a marker too, is read and stores nothing.

Seeded around the harness clock (Tuesday 1 September 2026, 12:00 UTC).
"""

from __future__ import annotations

from datetime import timedelta

from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.model import ASK_EXTRACTION
from tests.e2e.harness.process import GENERAL, NOW

PROPOSAL = "bora fazer o deploy na sexta?"
CONCLUSION = "fechou, deploy na sexta então"
CHATTER = "a máquina de café voltou a funcionar"


async def test_a_portuguese_decision_is_stored_and_a_proposal_is_not(bot: E2EBot) -> None:
    leo, bea = bot.person("Leo"), bot.person("Bea")
    bot.chat.script_decisions(
        CONCLUSION,
        {"summary": "O deploy passa a ser na sexta", "topic": "deploy", "confidence": 0.9},
    )

    await bot.chatter("general", bea, PROPOSAL, at=NOW - timedelta(minutes=3))
    await bot.chatter("general", leo, CONCLUSION, at=NOW - timedelta(minutes=2))
    await bot.chatter("general", bea, CHATTER, at=NOW - timedelta(minutes=1))

    # Both marker-bearing messages are read; the chatter carries no signal.
    assert await bot.extract_asks() == (ASK_EXTRACTION,) * 2

    rows = await bot.decision_rows()
    assert len(rows) == 1
    (row,) = rows
    assert row.source_message_id == bot.corpus[CONCLUSION]
    assert row.channel_id == GENERAL
    assert row.summary == "O deploy passa a ser na sexta"
    assert row.topic == "deploy"
    # The proposal was shown to the model as context, so the decision rests on
    # it: deleting it or opting out of it has to be able to reach this row.
    assert row.evidence_message_ids == [bot.corpus[CONCLUSION], bot.corpus[PROPOSAL]]
    assert row.embedded


async def test_a_proposal_alone_stores_no_decision(bot: E2EBot) -> None:
    bea = bot.person("Bea")

    await bot.chatter("general", bea, PROPOSAL, at=NOW - timedelta(minutes=1))

    assert await bot.extract_asks() == (ASK_EXTRACTION,)
    assert await bot.decision_rows() == []
