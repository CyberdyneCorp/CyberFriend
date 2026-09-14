"""One query, both kinds of evidence.

Conversation windows and document chunks are chunked differently and stored in
different tables, but they are embedded by the same model into the same vector
space, so a question about "the rollout plan" should reach the discussion and
the spec without the asker choosing which.

Fusion is by rank rather than score, for the same reason the conversational
legs fuse by rank: a cosine similarity over windows and one over document
chunks are not the same number even when they come from the same model,
because the lengths and the shapes of the text differ.

The viewer is required here too, and is passed to both backends. There is no
path through this module that searches anything unscoped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from chatmemory.app.documents.model import DocumentHit
from chatmemory.app.documents.ports import DocumentSearchBackend
from chatmemory.app.fusion import RRF_K
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery
from chatmemory.ports.store import SearchBackend


class EvidenceKind(StrEnum):
    CONVERSATION = "conversation"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class CorpusHit:
    """A result from either corpus, carrying what its citation needs.

    Kept as one type with a `kind` rather than a union, because every consumer
    downstream -- disclosure checks, citation rendering, fencing -- has to
    handle both, and a union invites a branch that forgets one of them.
    """

    kind: EvidenceKind
    channel: ChannelRef
    text: str
    score: float
    relevance_source: RelevanceSource
    window_id: int | None = None
    document_id: int | None = None
    chunk_id: int | None = None
    title: str | None = None
    location: str | None = None
    heading: str | None = None
    message_id: int | None = None
    source_url: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        """Identity for fusion. The two corpora have separate id spaces.

        Fusing on a bare integer would let window 7 and chunk 7 collide, and
        the collision would look like a very relevant result rather than a bug.
        """
        if self.kind is EvidenceKind.DOCUMENT:
            return (EvidenceKind.DOCUMENT.value, self.chunk_id or 0)
        return (EvidenceKind.CONVERSATION.value, self.window_id or 0)


def from_search_hit(hit: SearchHit) -> CorpusHit:
    return CorpusHit(
        kind=EvidenceKind.CONVERSATION,
        channel=hit.channel,
        text=hit.text,
        score=hit.score,
        relevance_source=hit.relevance_source,
        window_id=hit.window_id,
        message_id=hit.message_ids[0] if hit.message_ids else None,
    )


def from_document_hit(hit: DocumentHit) -> CorpusHit:
    return CorpusHit(
        kind=EvidenceKind.DOCUMENT,
        channel=hit.channel,
        text=hit.text,
        score=hit.score,
        relevance_source=RelevanceSource.VECTOR,
        document_id=hit.document_id,
        chunk_id=hit.chunk_id,
        title=hit.title,
        location=hit.location,
        heading=hit.heading,
        message_id=hit.entry_message_id,
        source_url=hit.source_url,
    )


def fuse(
    rankings: Sequence[Sequence[CorpusHit]], k: int = RRF_K, limit: int | None = None
) -> list[CorpusHit]:
    """Reciprocal rank fusion across corpora, keyed so ids cannot collide."""
    scores: dict[tuple[str, int], float] = {}
    best: dict[tuple[str, int], CorpusHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            scores[hit.key] = scores.get(hit.key, 0.0) + 1.0 / (k + rank + 1)
            best.setdefault(hit.key, hit)

    fused = [
        replace(best[key], score=score, relevance_source=RelevanceSource.FUSED_RRF)
        for key, score in scores.items()
    ]
    fused.sort(key=lambda h: (-h.score, h.key))
    return fused[:limit] if limit is not None else fused


def fuse_document_hits(
    rankings: Sequence[Sequence[DocumentHit]], k: int = RRF_K, limit: int | None = None
) -> list[DocumentHit]:
    """Fuse the lexical and vector legs of a document search.

    Lives here rather than in the adapter so there is one fusion rule in the
    system: a BM25 rank and a cosine rank are not comparable numbers, and the
    place that decides what to do about that should not be duplicated per
    backend.
    """
    scores: dict[int, float] = {}
    best: dict[int, DocumentHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank + 1)
            best.setdefault(hit.chunk_id, hit)
    fused = [replace(best[cid], score=score) for cid, score in scores.items()]
    fused.sort(key=lambda h: (-h.score, h.chunk_id))
    return fused[:limit] if limit is not None else fused


class CorpusSearch:
    """Searches conversation and documents together, always for a viewer."""

    def __init__(self, conversation: SearchBackend, documents: DocumentSearchBackend) -> None:
        self._conversation = conversation
        self._documents = documents

    async def search(self, viewer: Viewer, query: SearchQuery) -> list[CorpusHit]:
        """Both corpora, fused. `viewer` reaches both backends unchanged.

        Note what is *not* here: no widening of the viewer, and no permission
        field on the query. Each backend applies the predicate inside its own
        statement, and this layer only merges what came back.
        """
        if not viewer.visible_channels:
            return []
        conversation = [from_search_hit(h) for h in await self._conversation.search(viewer, query)]
        documents = [
            from_document_hit(h) for h in await self._documents.search_documents(viewer, query)
        ]
        return fuse([conversation, documents], limit=query.limit)

    async def search_documents(self, viewer: Viewer, query: SearchQuery) -> list[CorpusHit]:
        return [from_document_hit(h) for h in await self._documents.search_documents(viewer, query)]
