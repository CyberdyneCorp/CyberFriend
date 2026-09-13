"""Rank fusion, and the provenance that keeps its scores from being misread."""

from __future__ import annotations

from datetime import UTC, datetime

from chatmemory.app.fusion import RRF_K, reciprocal_rank_fusion
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.search import RelevanceSource, SearchHit

CHANNEL = ChannelRef("discord", 1)
T0 = datetime(2026, 9, 13, tzinfo=UTC)


def hit(window_id: int, score: float, source: RelevanceSource) -> SearchHit:
    return SearchHit(
        window_id=window_id,
        channel=CHANNEL,
        text=f"w{window_id}",
        starts_at=T0,
        ends_at=T0,
        score=score,
        relevance_source=source,
    )


def lexical(*ids: int) -> list[SearchHit]:
    return [hit(i, 10.0 - n, RelevanceSource.LEXICAL) for n, i in enumerate(ids)]


def vector(*ids: int) -> list[SearchHit]:
    return [hit(i, 0.9 - n / 100, RelevanceSource.VECTOR) for n, i in enumerate(ids)]


def test_agreement_between_rankings_wins() -> None:
    fused = reciprocal_rank_fusion([lexical(1, 2, 3), vector(3, 1, 2)])
    assert fused[0].window_id == 1


def test_result_in_only_one_ranking_still_appears() -> None:
    fused = reciprocal_rank_fusion([lexical(1), vector(2)])
    assert {h.window_id for h in fused} == {1, 2}


def test_fused_hits_are_tagged_as_fused() -> None:
    """A consumer must be able to tell an RRF score from a similarity."""
    fused = reciprocal_rank_fusion([lexical(1, 2), vector(2, 1)])
    assert all(h.relevance_source is RelevanceSource.FUSED_RRF for h in fused)


def test_first_place_score_is_small_by_construction() -> None:
    """The trap this provenance exists for: first place scores ~0.016.

    Anything treating that as a similarity would discard every result.
    """
    fused = reciprocal_rank_fusion([lexical(1)])
    assert fused[0].score == 1 / (RRF_K + 1)
    assert fused[0].score < 0.02


def test_ordering_is_deterministic_on_ties() -> None:
    a = reciprocal_rank_fusion([lexical(1, 2), vector(2, 1)])
    b = reciprocal_rank_fusion([lexical(1, 2), vector(2, 1)])
    assert [h.window_id for h in a] == [h.window_id for h in b]


def test_limit_truncates_after_fusion_not_before() -> None:
    fused = reciprocal_rank_fusion([lexical(1, 2, 3), vector(3, 2, 1)], limit=2)
    assert len(fused) == 2
    unlimited = reciprocal_rank_fusion([lexical(1, 2, 3), vector(3, 2, 1)])
    assert [h.window_id for h in fused] == [h.window_id for h in unlimited[:2]]


def test_empty_rankings_fuse_to_nothing() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []
