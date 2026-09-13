"""One query reaching both corpora, and the viewer reaching both backends.

The fusion detail that matters here is the key. Windows and chunks have
separate id spaces, so fusing on a bare integer makes window 7 and chunk 7 the
same result -- which looks like a very relevant hit rather than a bug.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from chatmemory.app.documents.model import DocumentHit
from chatmemory.app.documents.retrieval import (
    CorpusSearch,
    EvidenceKind,
    from_document_hit,
    from_search_hit,
    fuse,
    fuse_document_hits,
)
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery

OPEN = ChannelRef("discord", 100)
T0 = datetime(2026, 9, 13, tzinfo=UTC)


def window_hit(window_id: int, text: str = "we deploy on Thursday") -> SearchHit:
    return SearchHit(
        window_id=window_id,
        channel=OPEN,
        text=text,
        starts_at=T0,
        ends_at=T0,
        score=0.5,
        relevance_source=RelevanceSource.VECTOR,
    )


def document_hit(chunk_id: int, text: str = "the rollout plan") -> DocumentHit:
    return DocumentHit(
        chunk_id=chunk_id,
        document_id=1,
        channel=OPEN,
        text=text,
        location="p. 2",
        score=0.5,
        title="Rollout spec",
        entry_message_id=42,
    )


class FakeConversation:
    def __init__(self) -> None:
        self.viewers: list[Viewer] = []

    async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
        self.viewers.append(viewer)
        return [window_hit(7)]

    async def thread_context(
        self, viewer: Viewer, platform_message_id: int, radius: int = 10
    ) -> Sequence[Message]:  # pragma: no cover - unused here
        return []

    async def list_channels(self, viewer: Viewer) -> Sequence[ChannelRef]:  # pragma: no cover
        return []


class FakeDocuments:
    def __init__(self) -> None:
        self.viewers: list[Viewer] = []

    async def search_documents(
        self, viewer: Viewer, query: SearchQuery
    ) -> Sequence[DocumentHit]:
        self.viewers.append(viewer)
        return [document_hit(7)]


def viewer(*channel_ids: int) -> Viewer:
    return Viewer(
        person=PersonRef("discord", 1),
        visible_channels=frozenset(ChannelRef("discord", c) for c in channel_ids),
    )


async def test_a_query_matches_conversation_and_documents_together() -> None:
    conversation, documents = FakeConversation(), FakeDocuments()
    hits = await CorpusSearch(conversation, documents).search(
        viewer(100), SearchQuery(text="rollout")
    )

    kinds = {hit.kind for hit in hits}
    assert kinds == {EvidenceKind.CONVERSATION, EvidenceKind.DOCUMENT}
    assert all(hit.relevance_source is RelevanceSource.FUSED_RRF for hit in hits)


async def test_the_same_viewer_reaches_both_backends_unchanged() -> None:
    conversation, documents = FakeConversation(), FakeDocuments()
    who = viewer(100, 300)
    await CorpusSearch(conversation, documents).search(who, SearchQuery(text="rollout"))

    assert conversation.viewers == [who]
    assert documents.viewers == [who]


async def test_a_viewer_with_no_readable_channels_searches_nothing() -> None:
    conversation, documents = FakeConversation(), FakeDocuments()
    hits = await CorpusSearch(conversation, documents).search(
        viewer(), SearchQuery(text="rollout")
    )

    assert hits == []
    assert conversation.viewers == []
    assert documents.viewers == []


def test_a_window_and_a_chunk_with_the_same_id_are_different_results() -> None:
    fused = fuse([[from_search_hit(window_hit(7))], [from_document_hit(document_hit(7))]])
    assert len(fused) == 2


def test_a_document_hit_keeps_everything_a_citation_needs() -> None:
    hit = from_document_hit(document_hit(3))
    assert hit.title == "Rollout spec"
    assert hit.location == "p. 2"
    assert hit.message_id == 42  # the message that introduced the document


def test_document_legs_fuse_by_rank_not_by_score() -> None:
    """A BM25 rank and a cosine rank are not the same number."""
    lexical = [document_hit(1), document_hit(2)]
    vector = [document_hit(2), document_hit(3)]
    fused = fuse_document_hits([lexical, vector], limit=3)

    assert [hit.chunk_id for hit in fused][0] == 2  # ranked well by both legs
    assert len(fused) == 3
