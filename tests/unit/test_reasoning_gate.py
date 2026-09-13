"""The relevance gate: what it refuses to do, mostly.

The calibration numbers here are synthetic. Real thresholds require scoring
our own corpus with a real cross-encoder under production inference settings,
and until that exists the gate ships disabled -- which is what
`test_the_gate_is_off_by_default` pins down.
"""

from __future__ import annotations

import pytest

from chatmemory.app.reasoning.errors import ConfigurationError
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.gate import (
    UNCALIBRATED,
    Calibration,
    CalibrationSample,
    CoverageBucket,
    GateReason,
    RelevanceGate,
    check_refinement_context_budget,
    relevance_score,
)
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.search import RelevanceSource

CHANNEL = ChannelRef("discord", 100)


def item(window_id: int, score: float, source: RelevanceSource) -> Evidence:
    return Evidence(
        window_id=window_id,
        channel=CHANNEL,
        text="text",
        score=score,
        relevance_source=source,
    )


def samples() -> list[CalibrationSample]:
    """Four buckets. Partial is "one message on topic, the rest is noise"."""
    return [
        *(CalibrationSample(CoverageBucket.COVERED, s) for s in (0.62, 0.71, 0.88)),
        *(CalibrationSample(CoverageBucket.PARTIAL, s) for s in (0.48, 0.55, 0.66)),
        *(CalibrationSample(CoverageBucket.UNCOVERED_ADJACENT, s) for s in (0.31, 0.40)),
        *(CalibrationSample(CoverageBucket.UNCOVERED_FAR, s) for s in (0.02, 0.05, 0.09)),
    ]


def calibrated() -> Calibration:
    return Calibration.from_samples(samples())


# --- booting -----------------------------------------------------------


def test_the_gate_is_off_by_default() -> None:
    gate = RelevanceGate()
    assert not gate.enabled
    assert gate.calibration is UNCALIBRATED


def test_enabling_the_gate_without_a_calibrated_reranker_refuses_to_boot() -> None:
    with pytest.raises(ConfigurationError, match="calibrated for"):
        RelevanceGate(enabled=True)


def test_the_evaluation_bypass_needs_the_gate() -> None:
    with pytest.raises(ConfigurationError, match="bypass"):
        RelevanceGate(calibrated(), enabled=False, bypass_evaluation=True)


# --- which score may be thresholded ------------------------------------


def test_only_the_rerankers_own_score_is_ever_read() -> None:
    """A fused RRF score gives first place about 0.016. Thresholding it as if
    it were a probability is a silent misjudgement, so the accessor refuses."""
    calibration = calibrated()
    fused = item(1, 0.016, RelevanceSource.FUSED_RRF)
    reranked = item(2, 0.7, RelevanceSource.RERANKED)
    assert relevance_score(fused, calibration) is None
    assert relevance_score(reranked, calibration) == 0.7


def test_no_score_is_readable_from_an_uncalibrated_reranker() -> None:
    assert relevance_score(item(1, 0.9, RelevanceSource.RERANKED), UNCALIBRATED) is None


def test_a_candidate_without_a_reranker_score_makes_the_band_unreadable() -> None:
    gate = RelevanceGate(calibrated(), enabled=True, bypass_evaluation=True)
    mixed = [item(1, 0.01, RelevanceSource.RERANKED), item(2, 0.016, RelevanceSource.FUSED_RRF)]
    assert gate.all_below_low_band(mixed) is None
    assert gate.evaluate(mixed).reason is GateReason.MISSING_RERANKER_SCORE


# --- the band, and acting on it ----------------------------------------


def test_the_low_band_short_circuits_only_when_the_bypass_is_on() -> None:
    far_below = [item(1, 0.01, RelevanceSource.RERANKED), item(2, 0.02, RelevanceSource.RERANKED)]

    reading_only = RelevanceGate(calibrated(), enabled=True)
    assert reading_only.all_below_low_band(far_below) is True
    assert reading_only.evaluate(far_below).skip_evaluation is False
    assert reading_only.evaluate(far_below).reason is GateReason.BYPASS_DISABLED

    acting = RelevanceGate(calibrated(), enabled=True, bypass_evaluation=True)
    decision = acting.evaluate(far_below)
    assert decision.skip_evaluation
    assert decision.reason is GateReason.ALL_BELOW_LOW_BAND
    assert decision.model_calls == 0


def test_one_candidate_above_the_band_keeps_the_expensive_stage() -> None:
    gate = RelevanceGate(calibrated(), enabled=True, bypass_evaluation=True)
    mixed = [item(1, 0.01, RelevanceSource.RERANKED), item(2, 0.7, RelevanceSource.RERANKED)]
    decision = gate.evaluate(mixed)
    assert not decision.skip_evaluation
    assert decision.reason is GateReason.SOME_ABOVE_LOW_BAND
    assert decision.best_score == 0.7


# --- calibration -------------------------------------------------------


def test_the_low_threshold_clears_the_uncovered_far_maximum() -> None:
    calibration = calibrated()
    assert calibration.low_threshold > 0.09
    assert calibration.excluded_score == 0.09
    assert calibration.low_threshold < calibration.covered_floor


def test_a_high_threshold_must_clear_the_partial_maximum() -> None:
    """The 0.97-against-0.9735 mistake: a threshold equal to the score it
    exists to exclude does not exclude it."""
    calibration = calibrated()
    assert calibration.high_threshold is not None
    assert calibration.high_threshold > calibration.partial_maximum


def test_the_high_band_is_dropped_when_no_covered_sample_clears_partial() -> None:
    """Expected on this corpus: short windows put partial and covered on top
    of each other, and the honest answer is no usable high band."""
    overlapping = [
        CalibrationSample(CoverageBucket.COVERED, 0.50),
        CalibrationSample(CoverageBucket.COVERED, 0.55),
        # A partial window outscoring every covered one: exactly the length
        # effect our short messages are expected to produce.
        CalibrationSample(CoverageBucket.PARTIAL, 0.60),
        CalibrationSample(CoverageBucket.UNCOVERED_FAR, 0.05),
    ]
    assert Calibration.from_samples(overlapping).high_threshold is None


def test_calibration_fails_when_no_threshold_separates_the_buckets() -> None:
    inseparable = [
        CalibrationSample(CoverageBucket.COVERED, 0.30),
        CalibrationSample(CoverageBucket.UNCOVERED_FAR, 0.35),
    ]
    with pytest.raises(ConfigurationError, match="no separating low threshold"):
        Calibration.from_samples(inseparable)


def test_calibration_requires_both_of_the_decisive_buckets() -> None:
    with pytest.raises(ConfigurationError, match="covered and uncovered-far"):
        Calibration.from_samples([CalibrationSample(CoverageBucket.PARTIAL, 0.5)])


def test_the_rule_of_three_bound_is_reported_rather_than_implied() -> None:
    """Zero failures at n=12 bounds the true rate at ~25%: a clean sweep, not
    a validated margin."""
    assert Calibration.from_samples(samples()).sample_size == 11
    assert Calibration(sample_size=12).rule_of_three_bound == pytest.approx(0.25)
    assert Calibration(sample_size=0).rule_of_three_bound == 1.0


# --- the interaction that only exists when both are enabled ------------


def test_gate_plus_refinement_context_budget_is_checked_at_startup() -> None:
    with pytest.raises(ConfigurationError, match="unrefined size"):
        check_refinement_context_budget(
            gate_enabled=True,
            refinement_enabled=True,
            max_sources=10,
            max_chunk_tokens=1000,
            context_budget_tokens=4000,
        )


def test_neither_feature_alone_produces_the_starvation_risk() -> None:
    for gate_enabled, refinement_enabled in ((True, False), (False, True), (False, False)):
        check_refinement_context_budget(
            gate_enabled=gate_enabled,
            refinement_enabled=refinement_enabled,
            max_sources=10,
            max_chunk_tokens=1000,
            context_budget_tokens=4000,
        )
