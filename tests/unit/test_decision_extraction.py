"""Decisions, read in the ask pass: which messages reach the model, what is stored.

The candidate filter is English-only for asks, so the decision markers are the
only way a Portuguese conclusion nobody was addressed in is ever read. They are
tested here against the way people in this server actually write.
"""

from __future__ import annotations

import pytest

from chatmemory.app.asks.candidates import CandidateFilter
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import AddresseeSignal
from chatmemory.app.asks.resolution import StaticDirectory
from chatmemory.app.decisions.model import ExtractedDecision, decision_key, topic_slug
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    FakeAskStore,
    FakeDecisionStore,
    StubExtractor,
    extracted,
    message,
)

DIRECTORY = StaticDirectory({"alice": ALICE, "bob": BOB})

DEPLOY = ExtractedDecision(summary="O deploy passa a ser na sexta", topic="deploy", confidence=0.9)


def service(
    extractor: StubExtractor, decisions: FakeDecisionStore | None = None
) -> ExtractionService:
    return ExtractionService(
        extractor=extractor,
        store=FakeAskStore(),
        directory=DIRECTORY,
        decisions=decisions,
    )


# --- the signal -----------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "fechou, vamos com Postgres então",
        "fechado! deploy na sexta",
        "bora de Vercel mesmo",
        "ficou decidido: o deploy passa pra sexta",
        "tá decidido, sem reunião na segunda",
        "ta decidido entao",
        "decidimos manter o Redis",
        "combinado, eu subo amanhã",
        "vamos seguir com o plano B",
        "então fica assim, release quinzenal",
        "ficou definido que o onboarding é remoto",
        "we decided to ship on friday",
        "ok, let's go with the managed database",
        "lets go with option 2",
        "agreed, final call is monday",
    ],
)
def test_a_conclusion_is_a_candidate_in_either_language(content: str) -> None:
    assert CandidateFilter().signal(message(1, ALICE, content)) is AddresseeSignal.DECISION


@pytest.mark.parametrize(
    "content",
    [
        "que tal usar Postgres?",
        "acho que devíamos trocar de provedor",
        "o deploy quebrou de novo",
        "decididamente não gosto disso",
        "they disagreed about the schema",
    ],
)
def test_a_proposal_or_a_status_carries_no_decision_signal(content: str) -> None:
    """Silent outside its signals: these reach no model at all."""
    assert CandidateFilter().signal(message(1, ALICE, content)) is None


def test_a_mention_still_wins_over_the_decision_signal() -> None:
    """The stronger addressee signal is kept; the message is read either way."""
    found = CandidateFilter().signal(
        message(1, ALICE, "@bob fechou, vamos com Postgres", mentions=frozenset({BOB}))
    )
    assert found is AddresseeSignal.MENTION


# --- identity -------------------------------------------------------------


def test_the_key_comes_from_the_source_and_topic_never_the_summary() -> None:
    assert decision_key(10, "Deploy Day") == decision_key(10, "deploy  day")
    assert decision_key(10, "deploy") != decision_key(11, "deploy")


def test_a_portuguese_topic_keeps_its_accents_in_the_slug() -> None:
    assert topic_slug("Decisão de preço") == "decisão-de-preço"
    assert topic_slug("!!!") == "decision"


# --- recording ------------------------------------------------------------


async def test_a_decision_is_stored_against_the_message_that_concluded_it() -> None:
    decisions = FakeDecisionStore()
    proposal = message(9, BOB, "que tal deploy na sexta?")
    source = message(10, ALICE, "fechou, deploy na sexta")

    report = await service(StubExtractor(decisions=[DEPLOY]), decisions).extract_window(
        [proposal, source]
    )

    assert report.decisions == 1
    stored = decisions.decisions[decision_key(10, "deploy")]
    assert stored.source_message_id == 10
    assert stored.author == ALICE
    assert stored.decided_at == source.created_at
    assert stored.summary == DEPLOY.summary
    # The proposal was shown to the model, so it is evidence the decision
    # rests on: deleting it or opting out of it has to reach this row.
    assert stored.evidence_message_ids == (10, 9)


async def test_the_reply_parent_is_evidence_too() -> None:
    decisions = FakeDecisionStore()
    parent = message(9, BOB, "deploy na sexta?")
    reply = message(10, ALICE, "fechado", reply_to_id=9)

    await service(StubExtractor(decisions=[DEPLOY]), decisions).extract_window(
        [reply], parents={9: parent}
    )

    assert decisions.decisions[decision_key(10, "deploy")].evidence_message_ids == (10, 9)


async def test_a_re_run_that_finds_nothing_withdraws_the_decision() -> None:
    """How an edit that takes a decision back removes it."""
    decisions = FakeDecisionStore()
    source = message(10, ALICE, "fechou, deploy na sexta")
    await service(StubExtractor(decisions=[DEPLOY]), decisions).extract_window([source])

    edited = message(10, ALICE, "fechou? deploy na sexta ainda está em aberto")
    await service(StubExtractor(), decisions).extract_window([edited])

    assert decisions.decisions == {}


async def test_an_edit_that_removes_every_signal_still_withdraws_the_decision() -> None:
    """The edited message is no longer a candidate, so no model call prunes it."""
    decisions = FakeDecisionStore()
    source = message(10, ALICE, "fechou, deploy na sexta")
    await service(StubExtractor(decisions=[DEPLOY]), decisions).extract_window([source])

    edited = message(10, ALICE, "deploy talvez na sexta")
    extractor = StubExtractor()
    await service(extractor, decisions).extract_window([edited])

    assert extractor.calls == []
    assert decisions.decisions == {}
    assert decisions.withdrawn == [10]


async def test_the_same_topic_twice_in_one_message_is_one_decision() -> None:
    decisions = FakeDecisionStore()
    twice = [DEPLOY, ExtractedDecision("Deploy on Friday", "Deploy", 0.8)]
    source = message(10, ALICE, "fechou, deploy na sexta")

    report = await service(StubExtractor(decisions=twice), decisions).extract_window([source])

    assert report.decisions == 1
    assert len(decisions.decisions) == 1


async def test_a_decision_store_failure_does_not_cost_the_asks() -> None:
    extractor = StubExtractor(extracted(), decisions=[DEPLOY])
    asks = FakeAskStore()
    extraction = ExtractionService(
        extractor=extractor,
        store=asks,
        directory=DIRECTORY,
        decisions=FakeDecisionStore(fail=True),
    )
    source = message(10, ALICE, "@bob fechou, pode revisar?", mentions=frozenset({BOB}))

    report = await extraction.extract_window([source])

    assert report.recorded == 1
    assert len(asks.asks) == 1
    assert report.decisions_failed == 1


async def test_without_a_decision_store_decisions_are_dropped_quietly() -> None:
    report = await service(StubExtractor(decisions=[DEPLOY])).extract_window(
        [message(10, ALICE, "fechou, deploy na sexta")]
    )
    assert report.candidates == 1
    assert report.decisions == 0
    assert report.decisions_failed == 0
