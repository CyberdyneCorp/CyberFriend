"""The instrument, measured against hand-computed answers.

This is the part that has to be right before anything it reports can be
believed. Every expected value below is worked out by hand in the test name or
the comment beside it -- if a metric is re-derived in the test from the same
code that computes it, the test proves only that the code is consistent with
itself.

Runs in the ordinary suite. It costs nothing: no database, no embeddings, no
network.
"""

from __future__ import annotations

from chatmemory.app.evaluation.scoring import (
    AclBreach,
    GoldenReport,
    Judgement,
    Provenance,
    RankedResult,
    acl_breaches,
    score_question,
)

GENERAL, PRIVATE = 100, 300
READABLE = frozenset({GENERAL})
ORIGINS = {1: GENERAL, 2: GENERAL, 3: GENERAL, 9: PRIVATE}


def window(window_id: int, *message_ids: int, channel: int = GENERAL) -> RankedResult:
    return RankedResult(window_id=window_id, channel_id=channel, message_ids=message_ids)


def provenance() -> Provenance:
    return Provenance(
        measured_on="2026-09-16",
        embedding_model="test",
        embedding_dimensions=3,
        corpus_messages=0,
        corpus_windows=0,
        viewers=1,
    )


# --- recall and reciprocal rank -----------------------------------------


def test_one_of_two_expected_messages_at_rank_one_scores_half_recall_and_full_rr() -> None:
    score = score_question(
        "q", Judgement(expected=frozenset({1, 2})), [window(10, 1), window(11, 7)], k=10
    )
    assert score.recall_at_k == 0.5
    assert score.reciprocal_rank == 1.0
    assert score.first_relevant_rank == 1
    assert score.missed == (2,)


def test_the_answer_at_rank_four_scores_a_reciprocal_rank_of_one_quarter() -> None:
    ranked = [window(10, 7), window(11, 8), window(12, 9), window(13, 1)]
    score = score_question("q", Judgement(expected=frozenset({1})), ranked, k=10)
    assert score.reciprocal_rank == 0.25
    assert score.first_relevant_rank == 4
    assert score.recall_at_k == 1.0


def test_an_answer_below_the_cut_is_a_miss_and_not_a_smaller_score() -> None:
    """Rank 11 with k=10 is zero, not 1/11. The evidence never reaches the
    prompt, so counting it would credit retrieval for something unused."""
    ranked = [window(i, 90 + i) for i in range(10)] + [window(99, 1)]
    score = score_question("q", Judgement(expected=frozenset({1})), ranked, k=10)
    assert score.recall_at_k == 0.0
    assert score.reciprocal_rank == 0.0
    assert score.first_relevant_rank is None
    assert score.returned == 11, "the full list is still reported, only scoring is cut"


def test_a_window_carrying_the_answer_counts_however_many_other_messages_it_holds() -> None:
    """The retrieval unit is a window; the judgement names messages. A window
    is relevant when it carries one, not when it consists of one."""
    score = score_question(
        "q", Judgement(expected=frozenset({2})), [window(10, 1, 2, 3, 4, 5)], k=10
    )
    assert score.recall_at_k == 1.0
    assert score.reciprocal_rank == 1.0


def test_an_unanswerable_question_has_no_recall_and_no_reciprocal_rank() -> None:
    """`None`, not zero. Zero would drag the mean down for a question where
    the metric is undefined rather than badly served."""
    score = score_question("q", Judgement(forbidden=frozenset({3})), [window(10, 1)], k=10)
    assert score.answerable is False
    assert score.recall_at_k is None
    assert score.reciprocal_rank is None
    assert score.forbidden_at_k == ()


def test_a_forbidden_message_inside_the_cut_is_reported_as_a_trap() -> None:
    score = score_question(
        "q", Judgement(forbidden=frozenset({3})), [window(10, 3, 4)], k=10
    )
    assert score.forbidden_at_k == (3,)
    assert score.trapped


def test_a_forbidden_message_below_the_cut_is_not_a_trap() -> None:
    ranked = [window(i, 90 + i) for i in range(10)] + [window(99, 3)]
    score = score_question("q", Judgement(forbidden=frozenset({3})), ranked, k=10)
    assert score.forbidden_at_k == ()


def test_the_near_duplicate_beating_the_real_answer_is_reported_as_outranked() -> None:
    """The strong form of the trap measure.

    Membership in the top k is weak -- with few readable windows the whole
    corpus is the top k. This says the ranker preferred the confusable thread,
    which no corpus size explains away.
    """
    judgement = Judgement(expected=frozenset({1}), forbidden=frozenset({3}))
    score = score_question("q", judgement, [window(10, 3), window(11, 1)], k=10)
    assert score.first_forbidden_rank == 1
    assert score.first_relevant_rank == 2
    assert score.outranked_by_trap


def test_the_real_answer_above_its_near_duplicate_is_not_outranked() -> None:
    judgement = Judgement(expected=frozenset({1}), forbidden=frozenset({3}))
    score = score_question("q", judgement, [window(10, 1), window(11, 3)], k=10)
    assert score.trapped, "the trap is still present, and still worth reporting"
    assert not score.outranked_by_trap


def test_an_unanswerable_question_is_never_outranked() -> None:
    """There is nothing for the trap to beat, so the concept does not apply.
    Its `trap_rank` is the number to read instead."""
    score = score_question(
        "q", Judgement(forbidden=frozenset({3})), [window(10, 3)], k=10
    )
    assert score.first_forbidden_rank == 1
    assert not score.outranked_by_trap


# --- the permission dimension -------------------------------------------


def test_a_window_from_an_unreadable_channel_is_a_breach() -> None:
    found = acl_breaches("q", "sam", READABLE, ORIGINS, [window(10, 9, channel=PRIVATE)])
    assert len(found) == 1
    assert "may not read" in found[0].reason


def test_a_readable_window_citing_an_unreadable_message_is_also_a_breach() -> None:
    """The case a channel predicate alone cannot see: a window filed under a
    readable channel whose text came from somewhere else."""
    found = acl_breaches("q", "sam", READABLE, ORIGINS, [window(10, 1, 9)])
    assert len(found) == 1
    assert "message 9" in found[0].reason


def test_a_message_the_corpus_never_seeded_is_reported_rather_than_assumed_safe() -> None:
    """Fail closed. An unexplained id in a permission check is the last thing
    to shrug at."""
    found = acl_breaches("q", "sam", READABLE, ORIGINS, [window(10, 4242)])
    assert len(found) == 1
    assert "never seeded" in found[0].reason


def test_a_breach_below_the_scoring_depth_is_still_a_breach() -> None:
    ranked = [window(i, 1) for i in range(30)] + [window(99, 9, channel=PRIVATE)]
    assert acl_breaches("q", "sam", READABLE, ORIGINS, ranked)


def test_a_fully_readable_result_produces_no_breaches() -> None:
    assert acl_breaches("q", "sam", READABLE, ORIGINS, [window(10, 1, 2, 3)]) == ()


# --- the aggregate refuses to hide a breach ------------------------------


def test_breaches_never_move_recall_or_mrr() -> None:
    """The structural claim the whole design rests on: a report with a
    disclosure in it reports exactly the same quality numbers as one without,
    because the two dimensions share no arithmetic."""
    scores = (score_question("q", Judgement(expected=frozenset({1})), [window(10, 1)]),)
    clean = GoldenReport(k=10, provenance=provenance(), scores=scores)
    breached = GoldenReport(
        k=10,
        provenance=provenance(),
        scores=scores,
        breaches=(AclBreach("q", "sam", 99, PRIVATE, "leaked"),),
    )
    assert clean.mean_recall_at_k == breached.mean_recall_at_k == 1.0
    assert clean.mean_reciprocal_rank == breached.mean_reciprocal_rank == 1.0


def test_the_summary_leads_with_the_breach_count() -> None:
    """A reader who skims one line must still see it."""
    report = GoldenReport(
        k=10,
        provenance=provenance(),
        scores=(score_question("q", Judgement(expected=frozenset({1})), [window(10, 1)]),),
        breaches=(AclBreach("q", "sam", 99, PRIVATE, "leaked"),),
    )
    assert report.summary().startswith("acl=1 BREACHES")
    assert "BREACH q viewer=sam" in report.render()


def test_the_means_cover_answerable_questions_only() -> None:
    """An unanswerable question contributes to neither mean. Including it as
    a zero would make a set with more trick questions look worse at
    retrieval, which is not what the number claims to measure."""
    scores = (
        score_question("a", Judgement(expected=frozenset({1})), [window(10, 1)]),
        score_question("b", Judgement(forbidden=frozenset({3})), [window(11, 3)]),
    )
    report = GoldenReport(k=10, provenance=provenance(), scores=scores)
    assert report.mean_recall_at_k == 1.0
    assert report.mean_reciprocal_rank == 1.0
    assert report.clean_unanswerable == 0.0
    assert len(report.trapped) == 1
