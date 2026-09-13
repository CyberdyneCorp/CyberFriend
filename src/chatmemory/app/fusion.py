"""Reciprocal Rank Fusion over independently ranked result lists.

RRF combines rankings without needing their scores to be comparable, which is
exactly the situation: a BM25 score and a cosine distance mean different
things. It uses only rank.

The resulting score is NOT a probability or a similarity. With the
conventional k=60, a first-place result scores about 0.016 -- a number that
reads like "1.6% relevant" to anything that thresholds it naively. Every hit
therefore carries its `relevance_source` so a consumer can tell what kind of
number it is holding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from chatmemory.domain.search import RelevanceSource, SearchHit

RRF_K = 60


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchHit]], k: int = RRF_K, limit: int | None = None
) -> list[SearchHit]:
    """Fuse ranked lists. Input order within each list is the ranking."""
    scores: dict[int, float] = {}
    best: dict[int, SearchHit] = {}

    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            scores[hit.window_id] = scores.get(hit.window_id, 0.0) + 1.0 / (k + rank + 1)
            best.setdefault(hit.window_id, hit)

    fused = [
        replace(best[wid], score=score, relevance_source=RelevanceSource.FUSED_RRF)
        for wid, score in scores.items()
    ]
    fused.sort(key=lambda h: (-h.score, h.window_id))
    return fused[:limit] if limit is not None else fused
