"""Retrieval requests and results.

`SearchQuery` carries the asker's *intent* and nothing else. The viewer's
readable-channel predicate is deliberately not a field here: the corrective
loop added later rewrites queries to widen a search, and a "broaden the
filters" step must not be able to reach permissions. Authorization is applied
by the store from the viewer, which is passed separately and required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from chatmemory.domain.identity import ChannelRef, PersonRef


class RelevanceSource(StrEnum):
    """How a score was produced.

    Scores from different methods are different numbers and are never
    comparable. Reciprocal Rank Fusion in particular yields ~0.016 for a
    *first-place* result, which reads like "1.6% relevant" to anything that
    thresholds it without checking provenance.
    """

    LEXICAL = "lexical"
    VECTOR = "vector"
    FUSED_RRF = "fused_rrf"
    RERANKED = "reranked"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Intent only. Freely rewritable; contains no authorization."""

    text: str
    since: datetime | None = None
    until: datetime | None = None
    channels: frozenset[ChannelRef] | None = None  # preference, not permission
    authors: frozenset[PersonRef] | None = None
    limit: int = 20


@dataclass(frozen=True, slots=True)
class SearchHit:
    window_id: int
    channel: ChannelRef
    text: str
    starts_at: datetime
    ends_at: datetime
    score: float
    relevance_source: RelevanceSource
    message_ids: tuple[int, ...] = ()
