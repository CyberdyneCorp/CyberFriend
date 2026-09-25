"""The labelled set, the metrics over it, and the resolution accuracy it proves.

The model's own precision can only be measured against the configured endpoint
(see `tests/integration/test_asks_extraction_live.py`). What is measured here,
deterministically and for free, is everything downstream of the model: given
ideal extractions, does the pipeline attribute them to the right people?

That separation matters because addressee resolution is the likeliest source of
wrong entries, and a failure there would otherwise be indistinguishable from a
model failure in the live numbers.
"""

from __future__ import annotations

from chatmemory.app.asks.evaluation import (
    ADDRESSEE_GATE,
    DECISION_EXAMPLES,
    DECISION_PRECISION_GATE,
    EXAMPLES,
    GROUP,
    PEOPLE,
    PRECISION_GATE,
    Metrics,
    candidate_of,
    decision_candidate_of,
    directory,
    evaluate,
    evaluate_decisions,
    is_candidate,
)
from chatmemory.app.asks.model import Ask, AskKind, ExtractedAsk, ask_key
from chatmemory.app.asks.resolution import resolve_addressee
from chatmemory.app.decisions.model import ExtractedDecision

#: What an ideal model would return as the addressee for each positive example.
#: Written out rather than derived from the label, because the interesting case
#: is exactly where the two differ: "nadia" is what the message says, and
#: `None` is what the pipeline must record because she cannot be resolved.
IDEAL_HINTS: dict[str, str | None] = {
    "mention-request": "leo",
    "mention-question": "george",
    "reply-request": None,
    "reply-question": None,
    "named-request": "clinton",
    "commitment": None,
    "commitment-reply": None,
    "group-request": "the platform team",
    "group-question": "anyone",
    "unknown-name": "nadia",
}

GROUP_HINTS = {"group-request", "group-question"}


def test_every_example_is_labelled_consistently() -> None:
    ids = [e.example_id for e in EXAMPLES]
    assert len(ids) == len(set(ids))
    for example in EXAMPLES:
        assert example.author in PEOPLE
        assert all(m in PEOPLE for m in example.mentions)
        if example.expected is not None and example.expected.addressee not in (None, GROUP):
            assert example.expected.addressee in PEOPLE


def test_the_set_contains_more_negatives_than_positives() -> None:
    """Precision is the gate, so the set has to be able to fail on precision.

    A set of mostly-positives measures recall and calls it quality.
    """
    positives = sum(1 for e in EXAMPLES if e.expected is not None)
    assert positives < len(EXAMPLES) - positives


def test_every_positive_example_survives_the_cost_filter() -> None:
    """An ask the candidate filter drops is never extracted at any quality."""
    missed = [
        e.example_id
        for index, e in enumerate(EXAMPLES)
        if e.expected is not None and not is_candidate(e, index)
    ]
    assert missed == []


def test_most_negatives_reach_the_extractor() -> None:
    """Otherwise the precision measurement is flattered by the cost filter."""
    reaching = sum(
        1 for index, e in enumerate(EXAMPLES) if e.expected is None and is_candidate(e, index)
    )
    negatives = sum(1 for e in EXAMPLES if e.expected is None)
    assert reaching >= negatives - 2


def test_resolution_reaches_the_labelled_addressee_from_ideal_extractions() -> None:
    """Task 7.3, measured on its own: the model's job is held constant."""
    predictions: dict[str, list[Ask]] = {}
    for index, example in enumerate(EXAMPLES):
        if example.expected is None:
            continue
        candidate = candidate_of(example, index)
        extracted = ExtractedAsk(
            kind=example.expected.kind,
            text="the thing that was asked",
            confidence=0.9,
            addressee_hint=IDEAL_HINTS[example.example_id],
            addressee_is_group=example.example_id in GROUP_HINTS,
        )
        addressee = resolve_addressee(candidate, extracted, directory())
        predictions[example.example_id] = [
            Ask(
                key=ask_key(candidate.message.platform_message_id, extracted.kind, addressee),
                source_message_id=candidate.message.platform_message_id,
                channel=candidate.channel,
                requester=candidate.message.author,
                addressee=addressee,
                kind=extracted.kind,
                text=extracted.text,
                confidence=extracted.confidence,
                asked_at=candidate.message.created_at,
            )
        ]

    metrics = evaluate(predictions)
    assert metrics.addressee_accuracy == 1.0, metrics.report()
    assert metrics.recall == 1.0, metrics.report()


def test_precision_counts_an_ask_found_where_none_exists() -> None:
    metrics = evaluate({"thanks": [_any_ask()]})
    assert metrics.false_positives == 1
    assert metrics.precision == 0.0


def test_recall_counts_an_ask_that_was_missed() -> None:
    metrics = evaluate({})
    assert metrics.true_positives == 0
    assert metrics.false_negatives == sum(1 for e in EXAMPLES if e.expected is not None)


def test_the_wrong_kind_is_not_a_match() -> None:
    """A request recorded as a commitment tells somebody they promised something."""
    metrics = evaluate({"mention-request": [_any_ask(kind=AskKind.COMMITMENT)]})
    assert metrics.true_positives == 0
    assert metrics.false_positives == 1


def test_sub_threshold_extractions_are_not_scored() -> None:
    """They never reach an answer, so they cannot mislead anybody."""
    metrics = evaluate({"thanks": [_any_ask(confidence=0.1)]})
    assert metrics.false_positives == 0


def test_asserting_nothing_is_precise_not_wrong() -> None:
    assert Metrics().precision == 1.0


def test_the_gates_are_stated_and_favour_precision() -> None:
    assert PRECISION_GATE > ADDRESSEE_GATE


def _any_ask(kind: AskKind = AskKind.REQUEST, confidence: float = 0.9) -> Ask:
    from chatmemory.app.asks.evaluation import CHANNEL, T0
    from chatmemory.app.asks.model import to_person

    addressee = to_person(PEOPLE["leo"])
    return Ask(
        key=ask_key(1, kind, addressee),
        source_message_id=1,
        channel=CHANNEL,
        requester=PEOPLE["hezron"],
        addressee=addressee,
        kind=kind,
        text="something",
        confidence=confidence,
        asked_at=T0,
    )


# --- the decision set ------------------------------------------------------


def test_every_decision_example_is_labelled_consistently() -> None:
    ids = [e.example_id for e in DECISION_EXAMPLES]
    assert len(ids) == len(set(ids))
    for example in DECISION_EXAMPLES:
        assert example.author in PEOPLE
        assert all(author in PEOPLE for author, _ in example.context)


def test_the_decision_set_covers_both_languages_and_can_fail_on_precision() -> None:
    positives = [e for e in DECISION_EXAMPLES if e.decides]
    negatives = [e for e in DECISION_EXAMPLES if not e.decides]
    assert len(positives) <= len(negatives)
    for language in ("pt-", "en-"):
        assert any(e.example_id.startswith(language) for e in positives)
        assert any(e.example_id.startswith(language) for e in negatives)


def test_every_decision_survives_the_cost_filter() -> None:
    """A decision the filter drops is never recorded at any quality."""
    missed = []
    for index, example in enumerate(DECISION_EXAMPLES):
        if not example.decides:
            continue
        try:
            decision_candidate_of(example, index)
        except LookupError:
            missed.append(example.example_id)
    assert missed == []


def test_every_near_miss_reaches_the_extractor() -> None:
    """They share the markers, so the model -- not the filter -- is measured."""
    for index, example in enumerate(DECISION_EXAMPLES):
        if not example.decides:
            decision_candidate_of(example, index)


def test_the_proposal_is_shown_as_context_to_the_decision() -> None:
    index, example = next(
        (i, e) for i, e in enumerate(DECISION_EXAMPLES) if e.example_id == "pt-fechou"
    )
    candidate = decision_candidate_of(example, index)
    assert [m.content for m in candidate.context] == [c for _, c in example.context]


def test_a_decision_found_in_a_proposal_is_a_false_positive() -> None:
    metrics = evaluate_decisions({"pt-proposta": [_decision()]})
    assert metrics.false_positives == 1
    assert metrics.precision == 0.0


def test_ideal_decisions_meet_the_gate_and_weak_ones_are_not_scored() -> None:
    ideal = {e.example_id: [_decision()] for e in DECISION_EXAMPLES if e.decides}
    metrics = evaluate_decisions({**ideal, "pt-convite": [_decision(confidence=0.3)]})
    assert metrics.precision >= DECISION_PRECISION_GATE
    assert metrics.recall == 1.0


def _decision(confidence: float = 0.9) -> ExtractedDecision:
    return ExtractedDecision(summary="deploy na sexta", topic="deploy", confidence=confidence)
