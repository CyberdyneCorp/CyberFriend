"""The cheap signal that decides whether to pay for the expensive one.

Where a cross-encoder has already scored candidates, that score answers "is
any of this relevant?" for free. When every candidate scores far below the
answerable range, the LLM evaluation is several seconds and a few thousand
prompt tokens spent to conclude what the scores already showed.

Three things make that safe, and all three are enforced here rather than
described:

*   **Only a calibrated cross-encoder score is ever thresholded.** A single
    accessor, `relevance_score`, returns a number only when the score came
    from the reranker itself *and* the reranker is calibrated. A fused RRF
    score is not a probability -- first place is about 0.016 -- and feeding
    it to a probability-calibrated threshold is a silent misjudgement.
*   **The gate refuses to boot uncalibrated.** Enabling it without
    calibration is a configuration error, not a degraded mode.
*   **The evaluation bypass is a separate flag, off by default**, and the
    low-band primitive does not consult it. Reading the band is cheap and
    always allowed; acting on it by skipping a stage is the decision that
    needs its own switch.

Calibration method (see design.md): four buckets, not two. The two middle
buckets carry the whole method -- partial evidence scores high because it
genuinely answers part of the question, so a threshold calibrated against
uncovered data passes it as complete. For this corpus specifically, expect a
low covered floor and a wide spread: our windows are short, which is the
outlier case a document-corpus calibration flags as a length effect. The band
worth building here is the low one, whose failure mode is paying for a model
call that could have been skipped -- the right direction to fail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from chatmemory.app.reasoning.errors import ConfigurationError
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.domain.search import RelevanceSource

EPSILON = 1e-3
"""Separation added above an observed maximum.

A threshold set *equal* to the score it exists to exclude does not exclude
it. This is the 0.97-against-0.9735 mistake, in constant form.
"""


class CoverageBucket(StrEnum):
    """The four calibration buckets. Two buckets are not enough."""

    COVERED = "covered"
    PARTIAL = "partial"
    """One message in the window is on topic, the rest is noise."""

    UNCOVERED_ADJACENT = "uncovered_adjacent"
    """Topically adjacent but absent -- the source of the high outliers a
    naive gate mistakes for relevance."""

    UNCOVERED_FAR = "uncovered_far"


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    bucket: CoverageBucket
    score: float


@dataclass(frozen=True, slots=True)
class Calibration:
    """Thresholds, and the evidence for them.

    `excluded_score` records the specific observation the low threshold
    exists to sit above, and `sample_size` feeds the rule-of-three bound.
    Both are carried in the data rather than in a comment because the comment
    is what goes missing when the threshold is next adjusted.
    """

    low_threshold: float = 0.0
    high_threshold: float | None = None
    excluded_score: float = 0.0
    covered_floor: float = 0.0
    partial_maximum: float = 0.0
    sample_size: int = 0
    calibrated_for_answerability: bool = False
    notes: str = "uncalibrated: no cross-encoder samples supplied"

    @property
    def rule_of_three_bound(self) -> float:
        """Upper bound on the true error rate at 95% confidence given zero
        observed failures. Zero failures at n=12 bounds the rate at ~25%:
        a clean sweep, not a validated margin."""
        return 1.0 if self.sample_size <= 0 else min(1.0, 3.0 / self.sample_size)

    @classmethod
    def from_samples(cls, samples: Sequence[CalibrationSample]) -> Calibration:
        """Derive thresholds from scored, production-matched samples.

        The low threshold sits just above the uncovered-far maximum. The high
        threshold is emitted only if it clears the *partial* maximum and
        still leaves covered samples above it; on a short-message corpus it
        is usually unusable, and returning None is the honest outcome.

        Calibration scores a larger pool than production, whose candidates
        are the fused survivors of retrieval. Thresholds derived here
        therefore have headroom against what production sees, which argues
        for erring low rather than high.
        """
        by_bucket = {
            bucket: [s.score for s in samples if s.bucket is bucket] for bucket in CoverageBucket
        }
        covered = by_bucket[CoverageBucket.COVERED]
        far = by_bucket[CoverageBucket.UNCOVERED_FAR]
        partial = by_bucket[CoverageBucket.PARTIAL]
        if not covered or not far:
            raise ConfigurationError(
                "calibration needs covered and uncovered-far samples; "
                f"got {len(covered)} covered and {len(far)} uncovered-far"
            )

        far_max = max(far)
        low = far_max + EPSILON
        covered_floor = min(covered)
        if low >= covered_floor:
            raise ConfigurationError(
                f"no separating low threshold: uncovered-far reaches {far_max}, "
                f"but the covered floor is {covered_floor}; a gate here would "
                "skip answerable questions"
            )

        partial_max = max(partial) if partial else 0.0
        high = partial_max + EPSILON
        usable_high = high if partial and any(score > high for score in covered) else None

        return cls(
            low_threshold=low,
            high_threshold=usable_high,
            excluded_score=far_max,
            covered_floor=covered_floor,
            partial_maximum=partial_max,
            sample_size=len(samples),
            calibrated_for_answerability=True,
            notes=(
                f"low threshold {low:.4f} sits above the uncovered-far maximum "
                f"{far_max:.4f}; covered floor {covered_floor:.4f}; partial maximum "
                f"{partial_max:.4f}; n={len(samples)}"
            ),
        )


UNCALIBRATED = Calibration()
"""The default. The gate cannot be enabled against it."""


def relevance_score(item: Evidence, calibration: Calibration) -> float | None:
    """The only way a score reaches a threshold.

    Returns nothing unless the score is the reranker's own *and* the reranker
    is calibrated for answerability. Never reads a fused or fallback score:
    those are unbounded rank artefacts, and thresholding one feeds an
    arbitrary number into a probability-calibrated comparison.
    """
    if not calibration.calibrated_for_answerability:
        return None
    if item.relevance_source is not RelevanceSource.RERANKED:
        return None
    return item.score


class GateReason(StrEnum):
    DISABLED = "disabled"
    BYPASS_DISABLED = "bypass_disabled"
    MISSING_RERANKER_SCORE = "missing_reranker_score"
    ALL_BELOW_LOW_BAND = "all_below_low_band"
    SOME_ABOVE_LOW_BAND = "some_above_low_band"
    NO_CANDIDATES = "no_candidates"


@dataclass(frozen=True, slots=True)
class GateDecision:
    """A decision reached without a model. `model_calls` is zero by construction."""

    skip_evaluation: bool
    reason: GateReason
    best_score: float | None = None
    model_calls: int = 0


class RelevanceGate:
    """Reads the low band; skips the expensive stage only when told it may."""

    def __init__(
        self,
        calibration: Calibration = UNCALIBRATED,
        enabled: bool = False,
        bypass_evaluation: bool = False,
    ) -> None:
        if enabled and not calibration.calibrated_for_answerability:
            raise ConfigurationError(
                "the relevance gate requires a cross-encoder calibrated for "
                "answerability; the configured reranker is not calibrated "
                f"({calibration.notes})"
            )
        if bypass_evaluation and not enabled:
            raise ConfigurationError(
                "evaluation bypass requires the relevance gate to be enabled"
            )
        self._calibration = calibration
        self._enabled = enabled
        self._bypass = bypass_evaluation

    @property
    def calibration(self) -> Calibration:
        return self._calibration

    @property
    def enabled(self) -> bool:
        return self._enabled

    def all_below_low_band(self, items: Sequence[Evidence]) -> bool | None:
        """Whether every candidate scores below the low threshold.

        A primitive: it reports what the scores say and consults no feature
        flag. None means the question cannot be answered from these scores --
        no candidates, or no reranker score on one of them.
        """
        if not self._enabled or not items:
            return None
        scores = [relevance_score(item, self._calibration) for item in items]
        if any(score is None for score in scores):
            return None
        return all(score < self._calibration.low_threshold for score in scores if score is not None)

    def evaluate(self, items: Sequence[Evidence]) -> GateDecision:
        """Whether the expensive evaluation may be skipped for this candidate set."""
        if not self._enabled:
            return GateDecision(False, GateReason.DISABLED)
        if not items:
            return GateDecision(False, GateReason.NO_CANDIDATES)
        below = self.all_below_low_band(items)
        if below is None:
            return GateDecision(False, GateReason.MISSING_RERANKER_SCORE)
        best = max(
            score
            for score in (relevance_score(i, self._calibration) for i in items)
            if score is not None
        )
        if not below:
            return GateDecision(False, GateReason.SOME_ABOVE_LOW_BAND, best)
        if not self._bypass:
            # The band says nothing here is relevant, but skipping a stage on
            # that basis is a separate decision with its own switch.
            return GateDecision(False, GateReason.BYPASS_DISABLED, best)
        return GateDecision(True, GateReason.ALL_BELOW_LOW_BAND, best)


def check_refinement_context_budget(
    *,
    gate_enabled: bool,
    refinement_enabled: bool,
    max_sources: int,
    max_chunk_tokens: int,
    context_budget_tokens: int,
) -> None:
    """Startup check for the gate-plus-refinement interaction.

    When refinement allocates a shared context budget greedily and the gate
    skips an item, that item still costs its full unrefined size -- so one
    over-costed early source can push later, still-relevant ones out of the
    budget entirely. The risk exists only when both are enabled; neither
    alone produces it.
    """
    if not (gate_enabled and refinement_enabled):
        return
    worst_case = max_sources * max_chunk_tokens
    if worst_case > context_budget_tokens:
        raise ConfigurationError(
            f"gate and refinement are both enabled, so a skipped source still "
            f"costs its unrefined size: {max_sources} sources x {max_chunk_tokens} "
            f"tokens = {worst_case} exceeds the context budget of "
            f"{context_budget_tokens}"
        )
