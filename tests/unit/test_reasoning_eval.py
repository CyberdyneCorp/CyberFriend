"""Evaluation metrics, and the two rules that keep them honest."""

from __future__ import annotations

import pytest

from chatmemory.app.reasoning.contract import AnswerPath
from chatmemory.app.reasoning.eval import (
    AbstentionOutcome,
    DatasetMismatch,
    ExampleResult,
    RunReport,
    compare,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    score_abstention,
)

RELEVANT = {10, 20, 30}


def test_recall_and_precision_at_k() -> None:
    retrieved = [10, 99, 20, 98, 97]
    assert recall_at_k(retrieved, RELEVANT, 5) == pytest.approx(2 / 3)
    assert precision_at_k(retrieved, RELEVANT, 5) == pytest.approx(2 / 5)
    assert precision_at_k(retrieved, RELEVANT, 1) == 1.0
    assert recall_at_k([], RELEVANT, 5) == 0.0
    assert precision_at_k([], RELEVANT, 5) == 0.0


def test_reciprocal_rank_rewards_the_first_hit_only() -> None:
    assert reciprocal_rank([99, 10, 20], RELEVANT) == 0.5
    assert reciprocal_rank([98, 99], RELEVANT) == 0.0
    assert mean_reciprocal_rank([([10], RELEVANT), ([99, 10], RELEVANT)]) == 0.75
    assert mean_reciprocal_rank([]) == 0.0


def test_ndcg_rewards_rank_order() -> None:
    perfect = ndcg_at_k([10, 20, 30], RELEVANT, 3)
    worse = ndcg_at_k([99, 10, 20], RELEVANT, 3)
    assert perfect == 1.0
    assert 0.0 < worse < perfect
    assert ndcg_at_k([10], set(), 3) == 0.0


# --- abstention is scored, not punished --------------------------------


def test_abstaining_on_an_unanswerable_question_is_a_success() -> None:
    assert score_abstention(answerable=False, abstained=True) is (
        AbstentionOutcome.CORRECT_ABSTENTION
    )
    assert AbstentionOutcome.CORRECT_ABSTENTION.correct


def test_answering_an_unanswerable_question_is_confabulation() -> None:
    """The failure the whole module exists to keep visible: score abstention
    as failure and the cheapest way to stop failing is to invent."""
    outcome = score_abstention(answerable=False, abstained=False)
    assert outcome is AbstentionOutcome.CONFABULATION
    assert not outcome.correct


def test_the_two_answerable_cases_are_separated_too() -> None:
    assert score_abstention(answerable=True, abstained=False) is AbstentionOutcome.CORRECT_ANSWER
    assert score_abstention(answerable=True, abstained=True) is AbstentionOutcome.MISSED_ANSWER


def test_abstention_accuracy_counts_correct_abstentions_as_correct() -> None:
    report = RunReport(
        "golden-v1",
        (
            ExampleResult("a", abstention=AbstentionOutcome.CORRECT_ABSTENTION),
            ExampleResult("b", abstention=AbstentionOutcome.CORRECT_ANSWER),
            ExampleResult("c", abstention=AbstentionOutcome.CONFABULATION),
            ExampleResult("d", abstention=AbstentionOutcome.MISSED_ANSWER),
        ),
    )
    assert report.abstention_accuracy() == 0.5


# --- comparing runs ----------------------------------------------------


def result(example_id: str, recall: float, **kwargs: object) -> ExampleResult:
    return ExampleResult(example_id, {"recall@5": recall}, **kwargs)  # type: ignore[arg-type]


def test_comparing_across_dataset_versions_is_a_hard_error() -> None:
    with pytest.raises(DatasetMismatch):
        compare(RunReport("golden-v1"), RunReport("golden-v2"))


def test_per_example_deltas_show_what_a_mean_would_hide() -> None:
    """Helped on one, hurt on two -- an aggregate here reads as flat."""
    before = RunReport("golden-v1", (result("a", 0.5), result("b", 1.0), result("c", 1.0)))
    after = RunReport("golden-v1", (result("a", 1.0), result("b", 0.75), result("c", 0.75)))

    comparison = compare(before, after)
    assert [d.example_id for d in comparison.helped("recall@5")] == ["a"]
    assert [d.example_id for d in comparison.hurt("recall@5")] == ["b", "c"]
    assert sum(d.change for d in comparison.deltas) == 0.0


def test_metrics_and_examples_present_on_one_side_only_are_reported() -> None:
    before = RunReport(
        "golden-v1",
        (
            ExampleResult("a", {"recall@5": 1.0, "mrr": 1.0}),
            ExampleResult("gone", {"recall@5": 1.0}),
        ),
    )
    after = RunReport(
        "golden-v1",
        (
            ExampleResult("a", {"recall@5": 0.5, "ndcg@5": 0.9}),
            ExampleResult("new", {"recall@5": 1.0}),
        ),
    )

    comparison = compare(before, after)
    assert comparison.shared_metrics == ("recall@5",)
    assert comparison.only_in_before == ("mrr",)
    assert comparison.only_in_after == ("ndcg@5",)
    assert comparison.only_in_before_examples == ("gone",)
    assert comparison.only_in_after_examples == ("new",)


def test_cost_is_reported_per_path_so_the_loops_price_is_visible() -> None:
    report = RunReport(
        "golden-v1",
        (
            ExampleResult(
                "a", path=AnswerPath.FIXED, seconds=1.0, model_calls=2, prompt_tokens=900
            ),
            ExampleResult(
                "b", path=AnswerPath.LOOP, seconds=5.0, model_calls=6, prompt_tokens=4000
            ),
            ExampleResult(
                "c", path=AnswerPath.LOOP, seconds=7.0, model_calls=8, prompt_tokens=6000
            ),
        ),
    )
    fixed, loop = report.cost(AnswerPath.FIXED), report.cost(AnswerPath.LOOP)
    assert fixed["questions"] == 1.0
    assert loop["seconds"] == 6.0
    assert loop["model_calls"] == 7.0
    assert loop["prompt_tokens"] > fixed["prompt_tokens"]
