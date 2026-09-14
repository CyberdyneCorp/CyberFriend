"""The shared contract, and the provenance record that must not lie.

A provenance field is worth nothing if it is merely assigned. Athena has a
known case reporting a decision as model-made when no model call occurred,
which is exactly the moment the field is relied upon. Here the invariant is
enforced by the constructor, so a mis-attribution is a crash in a test rather
than a misleading row in production.
"""

from __future__ import annotations

import pytest

from chatmemory.app.reasoning.contract import (
    DEPENDENCY_FAILED_TEXT,
    NOTHING_FOUND,
    AnswerPath,
    Decision,
    DecisionMaker,
    LoggingRunRecorder,
    RunRecord,
    RunStatus,
    TerminalCause,
    abstention_answer,
    failure_answer,
)


def test_a_decision_attributed_to_a_model_must_have_cost_a_model_call() -> None:
    with pytest.raises(ValueError, match="attributed to a model"):
        Decision("sufficiency", "sufficient", DecisionMaker.MODEL, model_calls=0)


def test_a_cheap_signal_cannot_claim_model_calls() -> None:
    with pytest.raises(ValueError, match="cheap signal costs none"):
        Decision("sufficiency", "irrelevant", DecisionMaker.RELEVANCE_GATE, model_calls=1)


@pytest.mark.parametrize(
    "maker",
    [
        DecisionMaker.RELEVANCE_GATE,
        DecisionMaker.EMPTY_RESULT,
        DecisionMaker.POLICY,
        DecisionMaker.DRIVER,
        DecisionMaker.CLASSIFIER,
    ],
)
def test_every_non_model_maker_records_zero_model_calls(maker: DecisionMaker) -> None:
    assert Decision("d", "o", maker).model_calls == 0


def test_abstention_is_a_successful_status_and_failure_is_not() -> None:
    assert RunStatus.ABSTAINED.successful
    assert RunStatus.ANSWERED.successful
    assert not RunStatus.FAILED.successful


def test_abstention_and_failure_do_not_share_a_wording() -> None:
    assert abstention_answer().text == NOTHING_FOUND
    assert abstention_answer().abstained
    assert failure_answer().text == DEPENDENCY_FAILED_TEXT
    assert not failure_answer().abstained
    assert NOTHING_FOUND != DEPENDENCY_FAILED_TEXT


def test_the_record_totals_the_model_calls_of_its_decisions() -> None:
    record = RunRecord(
        path=AnswerPath.FIXED,
        status=RunStatus.ANSWERED,
        cause=TerminalCause.EVIDENCE_SUFFICIENT,
        decisions=(
            Decision("route", "fixed", DecisionMaker.CLASSIFIER),
            Decision("sufficiency", "sufficient", DecisionMaker.MODEL, model_calls=1),
        ),
    )
    assert record.model_calls == 1
    assert [d.name for d in record.decisions_named("route")] == ["route"]


def test_the_default_recorder_accepts_a_record() -> None:
    LoggingRunRecorder().record(
        RunRecord(
            path=AnswerPath.LOOP,
            status=RunStatus.ABSTAINED,
            cause=TerminalCause.ACCESS_BLOCKED,
        )
    )
