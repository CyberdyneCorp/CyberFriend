"""Measuring the thing, deterministically.

Two rules shape this module:

*   **Abstention is a scored outcome, not a failure.** If "I found nothing"
    counts against a configuration, tuning drives the system towards
    confabulation -- the fastest way to stop failing is to make something up.
    `AbstentionOutcome` separates the two errors that matter, and they are
    not symmetric: confabulating on an unanswerable question is far worse
    than abstaining on an answerable one.
*   **Report per-example deltas, not aggregates.** "Helped on 3, hurt on 7"
    is invisible in a mean, and a mean is what makes a regression look like
    an improvement.

Comparing two runs across different dataset versions is a hard error rather
than a caveat: the numbers are not comparable and a footnote will not stop
anyone quoting them.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from chatmemory.app.reasoning.contract import AnswerPath


def recall_at_k(retrieved: Sequence[int], relevant: Collection[int], k: int) -> float:
    if not relevant:
        return 0.0
    found = len(set(retrieved[:k]) & set(relevant))
    return found / len(set(relevant))


def precision_at_k(retrieved: Sequence[int], relevant: Collection[int], k: int) -> float:
    top = retrieved[:k]
    if not top:
        return 0.0
    return len(set(top) & set(relevant)) / len(top)


def reciprocal_rank(retrieved: Sequence[int], relevant: Collection[int]) -> float:
    for index, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / index
    return 0.0


def mean_reciprocal_rank(
    results: Sequence[tuple[Sequence[int], Collection[int]]]
) -> float:
    if not results:
        return 0.0
    return sum(reciprocal_rank(r, rel) for r, rel in results) / len(results)


def dcg_at_k(retrieved: Sequence[int], relevant: Collection[int], k: int) -> float:
    """Binary-gain DCG: a hit at rank i contributes 1 / log2(i + 1)."""
    return sum(
        1.0 / math.log2(index + 1)
        for index, item in enumerate(retrieved[:k], start=1)
        if item in relevant
    )


def ndcg_at_k(retrieved: Sequence[int], relevant: Collection[int], k: int) -> float:
    ideal = dcg_at_k(list(relevant)[:k], relevant, k)
    if ideal == 0.0:
        return 0.0
    return dcg_at_k(retrieved, relevant, k) / ideal


class AbstentionOutcome(StrEnum):
    """The four ways an answer-or-abstain decision can land."""

    CORRECT_ANSWER = "correct_answer"
    CORRECT_ABSTENTION = "correct_abstention"
    CONFABULATION = "confabulation"
    """Answered a question nothing in the corpus supports. The failure this
    whole module exists to keep visible."""

    MISSED_ANSWER = "missed_answer"

    @property
    def correct(self) -> bool:
        return self in (AbstentionOutcome.CORRECT_ANSWER, AbstentionOutcome.CORRECT_ABSTENTION)


def score_abstention(answerable: bool, abstained: bool) -> AbstentionOutcome:
    """Score one example. Abstaining on an unanswerable question is a success."""
    if answerable:
        return (
            AbstentionOutcome.MISSED_ANSWER if abstained else AbstentionOutcome.CORRECT_ANSWER
        )
    return (
        AbstentionOutcome.CORRECT_ABSTENTION if abstained else AbstentionOutcome.CONFABULATION
    )


@dataclass(frozen=True, slots=True)
class ExampleResult:
    """One example's outcome, keyed by a stable id so runs can be matched."""

    example_id: str
    metrics: Mapping[str, float] = field(default_factory=dict)
    abstention: AbstentionOutcome = AbstentionOutcome.CORRECT_ANSWER
    path: AnswerPath = AnswerPath.FIXED
    seconds: float = 0.0
    model_calls: int = 0
    prompt_tokens: int = 0


@dataclass(frozen=True, slots=True)
class RunReport:
    """One evaluation run, pinned to the dataset version it scored."""

    dataset_version: str
    results: tuple[ExampleResult, ...] = ()

    @property
    def by_id(self) -> Mapping[str, ExampleResult]:
        return {r.example_id: r for r in self.results}

    def abstention_accuracy(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.abstention.correct) / len(self.results)

    def cost(self, path: AnswerPath) -> Mapping[str, float]:
        """Per-question cost and latency for one path, so the loop's price is
        visible next to whatever quality it bought."""
        rows = [r for r in self.results if r.path is path]
        if not rows:
            return {"questions": 0.0, "seconds": 0.0, "model_calls": 0.0, "prompt_tokens": 0.0}
        count = float(len(rows))
        return {
            "questions": count,
            "seconds": sum(r.seconds for r in rows) / count,
            "model_calls": sum(r.model_calls for r in rows) / count,
            "prompt_tokens": sum(r.prompt_tokens for r in rows) / count,
        }


class DatasetMismatch(Exception):
    """Two runs scored different datasets. Their numbers are not comparable."""


@dataclass(frozen=True, slots=True)
class ExampleDelta:
    example_id: str
    metric: str
    before: float
    after: float

    @property
    def change(self) -> float:
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class Comparison:
    shared_metrics: tuple[str, ...]
    only_in_before: tuple[str, ...]
    only_in_after: tuple[str, ...]
    only_in_before_examples: tuple[str, ...]
    only_in_after_examples: tuple[str, ...]
    deltas: tuple[ExampleDelta, ...]

    def helped(self, metric: str) -> tuple[ExampleDelta, ...]:
        return tuple(d for d in self.deltas if d.metric == metric and d.change > 0)

    def hurt(self, metric: str) -> tuple[ExampleDelta, ...]:
        return tuple(d for d in self.deltas if d.metric == metric and d.change < 0)


def compare(before: RunReport, after: RunReport) -> Comparison:
    """Compare two runs example by example.

    Only the metric intersection is compared, and what each side had that the
    other lacked is reported rather than silently dropped -- a metric that
    appears in one run only is a change in what was measured, which is
    exactly the thing a comparison must not hide.
    """
    if before.dataset_version != after.dataset_version:
        raise DatasetMismatch(
            f"cannot compare runs over different datasets: "
            f"{before.dataset_version!r} and {after.dataset_version!r}"
        )
    before_by_id, after_by_id = before.by_id, after.by_id
    shared_ids = sorted(set(before_by_id) & set(after_by_id))

    before_metrics = {m for r in before.results for m in r.metrics}
    after_metrics = {m for r in after.results for m in r.metrics}
    shared_metrics = sorted(before_metrics & after_metrics)

    deltas = tuple(
        ExampleDelta(
            example_id=example_id,
            metric=metric,
            before=before_by_id[example_id].metrics[metric],
            after=after_by_id[example_id].metrics[metric],
        )
        for example_id in shared_ids
        for metric in shared_metrics
        if metric in before_by_id[example_id].metrics
        and metric in after_by_id[example_id].metrics
    )
    return Comparison(
        shared_metrics=tuple(shared_metrics),
        only_in_before=tuple(sorted(before_metrics - after_metrics)),
        only_in_after=tuple(sorted(after_metrics - before_metrics)),
        only_in_before_examples=tuple(sorted(set(before_by_id) - set(after_by_id))),
        only_in_after_examples=tuple(sorted(set(after_by_id) - set(before_by_id))),
        deltas=deltas,
    )
