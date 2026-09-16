"""Does retrieval find the *right* messages?

Until this file existed the only measured property of retrieval was that it
returned something. Every assertion here is about which something.

The order of the tests is the order of their seriousness, and the first one is
not a quality check. A viewer receiving a message they may not read is a
breach: it is not averaged, not weighted, and not traded off against recall,
because a mean that can absorb a disclosure is a mean that will eventually
report one as 0.94 and be believed.

Everything below that is a number with a floor. The floors are well under the
measured baseline in `BASELINE.md` -- they exist to catch a retrieval change
that breaks a question, not to freeze today's embedding model in place -- and
each failure names the question, because "recall fell" is not something anyone
can act on.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from chatmemory.app.evaluation.scoring import DEFAULT_K
from tests.evaluation import corpus
from tests.evaluation.harness import Sweep
from tests.evaluation.questions import QUESTIONS, Leg

pytestmark = pytest.mark.goldens

BASELINE = pathlib.Path(__file__).parent / "BASELINE.md"

# Floors, not targets. Set from the recorded baseline with room underneath, so
# a model upgrade that costs a little does not fail the build while a broken
# fusion or a dropped leg does.
MIN_MEAN_RECALL = 0.80
MIN_MEAN_RECIPROCAL_RANK = 0.65
MAX_RANK_OF_A_KNOWN_ANSWER = 8


# --- the dimension that is not a score ----------------------------------


def test_no_viewer_ever_received_a_message_they_may_not_read(measured: Sweep) -> None:
    """The breach check, and the reason it is first and on its own.

    Every question is asked as every viewer, not only as the viewer it was
    written for: the question that leaks is rarely the one somebody thought to
    ask as the wrong person. The whole result list is checked rather than the
    scored depth, because a leak at rank 17 discloses exactly as much as one
    at rank 1.
    """
    breaches = measured.report.breaches
    assert not breaches, (
        f"{len(breaches)} ACL BREACHES -- this is a disclosure, not a quality "
        "regression:\n" + "\n".join(b.line() for b in breaches)
    )


def test_every_viewer_could_reach_something_so_the_breach_check_means_something(
    measured: Sweep,
) -> None:
    """The breach check passes trivially against a corpus nobody can search.

    Without this, an empty index, a dead embedding endpoint or a resolver
    returning empty viewers would all report zero breaches and look like a
    clean bill of health.
    """
    assert measured.report.scores, "the sweep scored no questions at all"
    answered = [s for s in measured.report.answerable if s.first_relevant_rank is not None]
    assert len(answered) >= len(measured.report.answerable) // 2, (
        "more than half the answerable questions returned nothing relevant; "
        "the corpus or the embedding leg is not working, and the ACL result "
        "above is therefore meaningless"
    )


# --- the corpus this was measured against -------------------------------


def test_the_corpus_was_seeded_whole(measured: Sweep) -> None:
    """Messages present, windows absent or vectors absent is the state this
    project has shipped in before: healthy, ingesting, unable to answer."""
    seeded = measured.corpus
    assert seeded.messages == len(corpus.SEEDS)
    assert seeded.windows == len(corpus.CONVERSATIONS), (
        "each conversation should form exactly one window; if the windower "
        "changed, the judgements below are measuring something else"
    )
    assert seeded.embedded == seeded.windows, (
        "a window without a vector is a conversation the vector leg cannot reach"
    )


# --- relevance ----------------------------------------------------------


def test_mean_recall_and_mrr_hold_their_floor(measured: Sweep) -> None:
    report = measured.report
    assert report.mean_recall_at_k >= MIN_MEAN_RECALL, report.render()
    assert report.mean_reciprocal_rank >= MIN_MEAN_RECIPROCAL_RANK, report.render()


@pytest.mark.parametrize(
    "question_id",
    [q.id for q in QUESTIONS if q.judgement.answerable],
)
def test_each_answerable_question_still_finds_its_own_answer(
    measured: Sweep, question_id: str
) -> None:
    """Parametrised so a regression names the question in the failure line.

    A mean that slips from 0.93 to 0.88 says retrieval got worse. This says
    which question stopped working, which is the only form of that news anyone
    can act on.
    """
    score = next(s for s in measured.report.scores if s.question_id == question_id)
    question = next(q for q in QUESTIONS if q.id == question_id)

    assert score.first_relevant_rank is not None, (
        f"{question_id}: nothing in the top {DEFAULT_K} carried an expected "
        f"message. expected={sorted(question.judgement.expected)} "
        f"({question.why})"
    )
    assert score.first_relevant_rank <= MAX_RANK_OF_A_KNOWN_ANSWER, (
        f"{question_id}: the answer was found at rank {score.first_relevant_rank}, "
        f"below the cut an answering prompt would realistically read"
    )


def test_no_answerable_question_is_outranked_by_its_own_near_duplicate(
    measured: Sweep,
) -> None:
    """The near-duplicate pairs, stated as the comparison that matters.

    Whether the confusable thread appears at all depends on how much the
    viewer can read; whether it appears *above* the right answer does not. A
    ranker that prefers the search-latency incident to the payment one when
    asked about payments is wrong regardless of corpus size.
    """
    outranked = measured.report.outranked
    assert not outranked, "\n".join(s.line(measured.report.k) for s in outranked)


def test_a_question_with_no_answer_is_never_led_by_its_bait(measured: Sweep) -> None:
    """The hard claim about a question the corpus cannot answer.

    Not "it returned nothing" -- retrieval has no similarity floor, so the
    vector leg returns its nearest neighbours whether or not any is relevant,
    and an empty result would never happen. Not "the bait stayed out of the
    top ten" either: whether it appears at all depends on how much of the
    corpus the viewer can read, which is a property of the membership rather
    than of the ranker. What the ranker owns is which result it puts first.

    The softer figure, how often the bait stayed out of the scored depth
    entirely, is recorded in `BASELINE.md` and deliberately not gated -- it is
    a proportion over three questions, so any threshold on it would move on
    noise. Today it is 1/3, and the two questions that trip it are named there
    rather than averaged away.
    """
    rows = measured.report.unanswerable
    assert rows, "the set is supposed to contain questions with no right answer"
    led = measured.report.bait_at_the_top
    assert not led, "\n".join(s.line(measured.report.k) for s in led)


def test_the_same_question_gets_a_different_right_answer_per_viewer(
    measured: Sweep,
) -> None:
    """The two halves of the office question are word-for-word identical.

    If both viewers score, the corpus really does hold two different truths
    and retrieval really is returning the one each person is entitled to --
    which no single-viewer metric can distinguish from returning the same
    thing to everybody.
    """
    lead = next(q for q in QUESTIONS if q.id == "office-change-lead")
    staff = next(q for q in QUESTIONS if q.id == "office-change-staff")
    assert lead.text == staff.text
    assert not (lead.judgement.expected & staff.judgement.expected)

    for question_id in ("office-change-lead", "office-change-staff"):
        score = next(s for s in measured.report.scores if s.question_id == question_id)
        assert score.recall_at_k == 1.0, score.line(measured.report.k)


# --- the set is still testing what it says it tests ----------------------


def test_questions_declared_answerable_only_by_meaning_still_are(
    measured: Sweep,
) -> None:
    """The check that keeps this from becoming a keyword test.

    A vector question is one whose wording shares no searchable stem with its
    own answer. That is a property of the corpus text, not of the code, and it
    quietly stops being true the moment somebody rewords a seeded message. So
    the lexical statement is run alone: if it can find the answer, the
    question has stopped measuring semantic retrieval and says so here rather
    than inflating the score forever.
    """
    leaked = [
        outcome
        for outcome in measured.legs
        if outcome.declared is Leg.VECTOR and outcome.lexical_found
    ]
    assert not leaked, (
        "these questions claim to be answerable only by meaning, but the "
        "lexical leg found their answers: "
        f"{[o.question_id for o in leaked]}"
    )

    missed = [
        outcome
        for outcome in measured.legs
        if outcome.declared is Leg.VECTOR and not outcome.vector_found
    ]
    assert not missed, (
        "the vector leg alone could not answer questions that only it can "
        f"answer: {[o.question_id for o in missed]}"
    )


def test_questions_that_hang_on_an_exact_token_are_found_lexically(
    measured: Sweep,
) -> None:
    """The converse is deliberately not asserted.

    Whether the embedding model also happens to place a bare error code near
    its conversation is a property of the model, and freezing it would make
    the set fail on an unrelated upgrade. What must hold is that the lexical
    leg -- the reason an error code is findable at all -- still finds it.
    """
    missed = [
        outcome
        for outcome in measured.legs
        if outcome.declared is Leg.LEXICAL and not outcome.lexical_found
    ]
    assert not missed, (
        "an exact identifier was not found by the lexical leg: "
        f"{[o.question_id for o in missed]}"
    )


# --- provenance ---------------------------------------------------------


def test_the_run_is_recorded_with_its_provenance(measured: Sweep) -> None:
    """A number with no provenance cannot be compared against anything.

    The baseline file is written from this report rather than typed, so the
    date and the model recorded next to a number are always the ones that
    produced it. Re-record with GOLDENS_WRITE_BASELINE=1.
    """
    report = measured.report
    assert report.provenance.embedding_model
    assert report.provenance.measured_on

    if os.environ.get("GOLDENS_WRITE_BASELINE", "").strip() in {"1", "true", "yes"}:
        BASELINE.write_text(_baseline_document(report.render()))


RECORDED = "<!-- recorded-run -->"
"""Everything above this line is prose and is preserved; everything below is
regenerated. Split on an explicit sentinel rather than on the first fence: the
prose above contains fenced commands, and splitting on those silently ate most
of the document the first time."""


def _baseline_document(rendered: str) -> str:
    existing = BASELINE.read_text() if BASELINE.exists() else ""
    head, marker, _ = existing.partition(RECORDED)
    if not marker:
        raise AssertionError(
            f"{BASELINE} has no {RECORDED} sentinel, so the run cannot be "
            "recorded without overwriting the prose that explains it"
        )
    return f"{head}{RECORDED}\n\n```\n{rendered}\n```\n"
