"""Ports for document ingestion, parsing, fetching and retrieval.

The same asymmetry as `ports.store`: writes take what they need, and every
method that returns document content takes a `Viewer` as a required positional
argument. An unfiltered read of the document corpus is not expressible here.

`DocumentParser` returns a `ParseOutcome` rather than raising, because a
hostile file is an ordinary input on this path. An implementation that raises
on malformed input forces every caller to remember a try/except, and the one
that forgets stops ingestion for everybody.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from chatmemory.app.documents.model import (
    Document,
    DocumentChunk,
    DocumentCost,
    DocumentEntry,
    DocumentFormat,
    DocumentHit,
    FetchRecord,
    ParseOutcome,
    StoredDocument,
)
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import SearchQuery


class DocumentParser(Protocol):
    """Extracts text from one file, under limits, without trusting it."""

    async def parse(self, data: bytes, fmt: DocumentFormat, filename: str) -> ParseOutcome:
        """Return extracted text, or the reason the file was skipped.

        Must not raise for malformed input, must not resolve external entities,
        and must return within the configured time limit whatever the file
        does -- which means killing the parser, not waiting for it.
        """
        ...


class ContentFetcher(Protocol):
    """Retrieves attachment bytes at ingest time."""

    async def fetch(self, url: str, max_bytes: int, timeout: float) -> bytes | None:
        """Bytes, or None if they could not be retrieved within the limits.

        None rather than an exception: a failed attachment is a skipped
        attachment, never a failed message.
        """
        ...


class FetchedExternal(Protocol):
    """What an external system returned for a link."""

    @property
    def data(self) -> bytes: ...

    @property
    def filename(self) -> str: ...

    @property
    def media_type(self) -> str: ...

    @property
    def revision(self) -> str | None: ...

    @property
    def containers(self) -> frozenset[str]: ...


class ExternalDocumentSource(Protocol):
    """A read-only reader for one configured external system.

    Read-only is a property of the adapter, not a runtime check: there is no
    write method on this protocol to call.
    """

    @property
    def name(self) -> str: ...

    async def fetch(self, url: str, max_bytes: int, timeout: float) -> FetchedExternal | None:
        """The document, or None if it could not be retrieved.

        None covers "no such document", "not shared with this credential" and
        "refused" without distinguishing them, because the distinction is
        itself a disclosure: it confirms that a document exists.
        """
        ...


class DocumentStore(Protocol):
    """Persistence for documents, their entries and their chunks."""

    async def upsert_document(self, document: Document) -> StoredDocument:
        """Insert or update by identity, reporting whether the content changed.

        Identity is the bytes for an attachment and the URI for an external
        document; see `Document.identity`.
        """
        ...

    async def document_content_hash(self, identity: str) -> str | None:
        """The hash we last indexed for this identity, if we hold it.

        Lets an unchanged document skip parsing and re-embedding entirely,
        which matters because one document can cost more to embed than a month
        of the channel's conversation.
        """
        ...

    async def record_entry(self, document_id: int, entry: DocumentEntry) -> None:
        """Record one act of sharing, and widen the document's visibility.

        Entries are additive: a document shared into a second channel becomes
        readable by that channel's readers too, and stays readable by the
        first channel's.
        """
        ...

    async def replace_chunks(self, document_id: int, chunks: Sequence[DocumentChunk]) -> int:
        """Rebuild a document's chunks, discarding the previous ones."""
        ...

    async def tombstone_document(self, document_id: int, at: datetime) -> None:
        """Stop returning a document everywhere, immediately."""
        ...

    async def tombstone_entries_for_message(self, message_id: int, at: datetime) -> int:
        """Withdraw the shares a deleted message made.

        A document with no live entries left is no longer returned at all; one
        that also entered elsewhere stays readable through that other channel.
        """
        ...

    async def purge_channel_documents(self, channel: ChannelRef) -> int:
        """Remove a channel's entries when it leaves indexing scope."""
        ...

    async def purge_person_documents(self, person: PersonRef) -> int:
        """Opt-out: remove documents a person introduced, not only their messages."""
        ...

    async def purge_documents_before(self, cutoff: datetime) -> int:
        """Retention: documents that entered before the cutoff."""
        ...

    async def record_fetch(self, record: FetchRecord) -> None:
        """Audit one fetch attempt: what, from where, and which message linked it."""
        ...

    async def fetch_attempts(self, target: str) -> int:
        """How many times this target has already failed, so retries can stop."""
        ...

    async def chunks_missing_embeddings(
        self, limit: int
    ) -> Sequence[tuple[int, str]]: ...

    async def store_chunk_embedding(self, chunk_id: int, embedding: Sequence[float]) -> None: ...

    async def external_documents_due(self, limit: int) -> Sequence[Document]:
        """Indexed external documents, oldest fetch first, for reconciliation."""
        ...

    async def document_cost(self) -> DocumentCost:
        """Embedding volume attributable to documents, and to conversation."""
        ...


class DocumentSearchBackend(Protocol):
    """Retrieval over document chunks. The viewer is required, as everywhere."""

    async def search_documents(
        self, viewer: Viewer, query: SearchQuery
    ) -> Sequence[DocumentHit]:
        """Chunks from documents that entered through a channel the viewer reads.

        The viewer's channel set is bound into the same statement as the
        ranking. Filtering an approximate scan afterwards under-returns, and
        does so worst for the people in the fewest channels.
        """
        ...
