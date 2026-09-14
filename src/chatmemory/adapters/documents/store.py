"""Postgres implementation of the document store and document retrieval.

The interesting work is in `documents_sql`; this is the adapter that binds
parameters to it and turns rows back into domain objects. Two things in here
are load-bearing rather than mechanical:

  * every write that changes which channels a document entered through calls
    `REFRESH_CHUNK_CHANNELS` afterwards, because that column *is* the
    permission predicate and a stale one is a disclosure or a silent loss;

  * retrieval takes the viewer first and positionally, so a call site that
    forgets the scope does not compile rather than reading everything.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from chatmemory.adapters.store import documents_sql as dsql
from chatmemory.adapters.store import sql
from chatmemory.app.documents.model import (
    Document,
    DocumentChunk,
    DocumentCost,
    DocumentEntry,
    DocumentFormat,
    DocumentHit,
    DocumentOrigin,
    FetchRecord,
    StoredDocument,
)
from chatmemory.app.documents.ports import DocumentSearchBackend, DocumentStore
from chatmemory.app.documents.retrieval import fuse_document_hits
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import SearchQuery
from chatmemory.ports.sources import EmbeddingClient

log = structlog.get_logger()

PLATFORM = "discord"

# Matches app.windowing.approximate_tokens: cost reporting has to use the same
# estimate as sizing, or the two numbers describe different worlds.
CHARS_PER_TOKEN = 3


def _channel_ids(viewer: Viewer) -> list[int]:
    return [c.platform_channel_id for c in viewer.visible_channels]


async def _person_id(conn: AsyncConnection, person: PersonRef) -> int:
    """Resolve a platform identity to a person row, creating it if unseen.

    Deliberately mirrors `PostgresStore._person_id`: an uploader who has never
    been seen as a message author still has to be resolvable, or the
    person-level opt-out has nothing to key on.
    """
    row = await conn.execute(
        text(
            "SELECT person_id FROM person_platform_id "
            "WHERE platform = :p AND platform_user_id = :u"
        ),
        {"p": person.platform, "u": person.platform_user_id},
    )
    existing = row.scalar()
    if existing is not None:
        return int(existing)

    created = await conn.execute(
        text("INSERT INTO person (display_name) VALUES (:n) RETURNING id"),
        {"n": str(person.platform_user_id)},
    )
    person_id = int(created.scalar_one())
    await conn.execute(
        text(
            "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
            "VALUES (:p, :u, :i) ON CONFLICT DO NOTHING"
        ),
        {"p": person.platform, "u": person.platform_user_id, "i": person_id},
    )
    return person_id


class PostgresDocumentStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert_document(self, document: Document) -> StoredDocument:
        async with self._engine.begin() as conn:
            previous = await self._content_hash(conn, document.identity)
            row = await conn.execute(
                dsql.UPSERT_DOCUMENT,
                {
                    "identity_key": document.identity,
                    "content_hash": document.content_hash,
                    "origin": document.origin.value,
                    "media_type": document.media_type.value,
                    "title": document.title,
                    "byte_size": document.byte_size,
                    "external_uri": document.external_uri,
                    "source_revision": document.source_revision,
                    "metadata": json.dumps(dict(document.metadata)),
                    "fetched_at": document.fetched_at,
                },
            )
            document_id = int(row.scalar_one())
        return StoredDocument(document_id, content_changed=previous != document.content_hash)

    async def document_content_hash(self, identity: str) -> str | None:
        async with self._engine.connect() as conn:
            return await self._content_hash(conn, identity)

    async def _content_hash(self, conn: AsyncConnection, identity: str) -> str | None:
        row = await conn.execute(dsql.DOCUMENT_CONTENT_HASH, {"identity_key": identity})
        value = row.scalar()
        return None if value is None else str(value)

    async def record_entry(self, document_id: int, entry: DocumentEntry) -> None:
        async with self._engine.begin() as conn:
            uploader = None if entry.uploader is None else await _person_id(conn, entry.uploader)
            await conn.execute(
                dsql.RECORD_ENTRY,
                {
                    "document_id": document_id,
                    "channel_id": entry.channel.platform_channel_id,
                    "message_id": entry.message_id,
                    "uploader_person_id": uploader,
                    "attachment_id": entry.attachment_id,
                    "source_url": entry.source_url or "",
                    "entered_at": entry.entered_at,
                },
            )
            # The share has happened; the chunks must become visible through
            # that channel in the same transaction, not on some later pass.
            await conn.execute(dsql.REFRESH_CHUNK_CHANNELS, {"document_id": document_id})

    async def replace_chunks(self, document_id: int, chunks: Sequence[DocumentChunk]) -> int:
        async with self._engine.begin() as conn:
            await conn.execute(dsql.DELETE_CHUNKS, {"document_id": document_id})
            for chunk in chunks:
                await conn.execute(
                    dsql.INSERT_CHUNK,
                    {
                        "document_id": document_id,
                        "ordinal": chunk.ordinal,
                        "text": chunk.text,
                        "location": chunk.location,
                        "heading": chunk.heading,
                    },
                )
            await conn.execute(dsql.REFRESH_CHUNK_CHANNELS, {"document_id": document_id})
        return len(chunks)

    async def tombstone_document(self, document_id: int, at: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(dsql.TOMBSTONE_DOCUMENT, {"document_id": document_id, "at": at})
            await conn.execute(
                dsql.TOMBSTONE_DOCUMENT_CHUNKS, {"document_id": document_id, "at": at}
            )

    async def tombstone_entries_for_message(self, message_id: int, at: datetime) -> int:
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                dsql.TOMBSTONE_ENTRIES_FOR_MESSAGE, {"message_id": message_id, "at": at}
            )
            affected = [int(r[0]) for r in rows]
            await self._refresh(conn, affected)
        return len(affected)

    async def purge_channel_documents(self, channel: ChannelRef) -> int:
        return await self._purge(
            dsql.PURGE_CHANNEL_ENTRIES, {"channel_id": channel.platform_channel_id}
        )

    async def purge_person_documents(self, person: PersonRef) -> int:
        async with self._engine.begin() as conn:
            person_id = await _person_id(conn, person)
            rows = await conn.execute(dsql.PURGE_PERSON_ENTRIES, {"person_id": person_id})
            affected = [int(r[0]) for r in rows]
            await self._refresh(conn, affected)
            await conn.execute(dsql.DELETE_ORPHANED_DOCUMENTS)
        return len(affected)

    async def purge_documents_before(self, cutoff: datetime) -> int:
        return await self._purge(dsql.PURGE_ENTRIES_BEFORE, {"cutoff": cutoff})

    async def _purge(self, statement: TextClause, params: dict[str, object]) -> int:
        async with self._engine.begin() as conn:
            rows = await conn.execute(statement, params)
            affected = [int(r[0]) for r in rows]
            await self._refresh(conn, affected)
            # A document nobody shares any more is not kept for its own sake;
            # its chunks go with it by cascade.
            await conn.execute(dsql.DELETE_ORPHANED_DOCUMENTS)
        return len(affected)

    async def _refresh(self, conn: AsyncConnection, document_ids: Iterable[int]) -> None:
        for document_id in set(document_ids):
            await conn.execute(dsql.REFRESH_CHUNK_CHANNELS, {"document_id": document_id})

    async def record_fetch(self, record: FetchRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                dsql.RECORD_FETCH,
                {
                    "target": record.target,
                    "outcome": record.outcome,
                    "channel_id": record.channel.platform_channel_id,
                    "message_id": record.message_id,
                    "document_hash": record.document_hash,
                    "attempts": record.attempts,
                    "at": record.at,
                },
            )

    async def fetch_attempts(self, target: str) -> int:
        async with self._engine.connect() as conn:
            row = await conn.execute(dsql.FETCH_ATTEMPTS, {"target": target})
            return int(row.scalar() or 0)

    async def chunks_missing_embeddings(self, limit: int) -> Sequence[tuple[int, str]]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(dsql.CHUNKS_MISSING_EMBEDDINGS, {"limit": limit})
            return [(int(r[0]), str(r[1])) for r in rows]

    async def store_chunk_embedding(self, chunk_id: int, embedding: Sequence[float]) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                dsql.STORE_CHUNK_EMBEDDING,
                {"id": chunk_id, "embedding": sql.vector_literal(embedding)},
            )

    async def external_documents_due(self, limit: int) -> Sequence[Document]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(dsql.EXTERNAL_DOCUMENTS_DUE, {"limit": limit})
            return [_document(r) for r in rows.mappings()]

    async def document_cost(self) -> DocumentCost:
        async with self._engine.connect() as conn:
            row = (await conn.execute(dsql.EMBEDDING_COST)).mappings().one()
            return DocumentCost(
                documents=int(row["documents"]),
                chunks=int(row["chunks"]),
                estimated_tokens=int(row["document_chars"]) // CHARS_PER_TOKEN,
                conversation_windows=int(row["windows"]),
                conversation_estimated_tokens=int(row["window_chars"]) // CHARS_PER_TOKEN,
            )


def _document(row: RowMapping) -> Document:
    return Document(
        content_hash=str(row["content_hash"]),
        origin=DocumentOrigin(str(row["origin"])),
        media_type=DocumentFormat(str(row["media_type"])),
        byte_size=int(row["byte_size"]),
        title=cast("str | None", row["title"]),
        external_uri=cast("str | None", row["external_uri"]),
        source_revision=cast("str | None", row["source_revision"]),
        fetched_at=cast("datetime | None", row["fetched_at"]),
        document_id=int(row["id"]),
    )


class DocumentSearch:
    """Lexical and vector retrieval over document chunks, fused.

    Both legs bind the viewer's channel set into their own statement, so each
    scan is constrained rather than its output filtered.
    """

    def __init__(
        self, engine: AsyncEngine, embeddings: EmbeddingClient, overfetch: int = 3
    ) -> None:
        self._engine = engine
        self._embeddings = embeddings
        self._overfetch = overfetch

    async def search_documents(self, viewer: Viewer, query: SearchQuery) -> Sequence[DocumentHit]:
        channels = _channel_ids(viewer)
        if not channels:
            # Nothing readable means nothing to search. An unconstrained query
            # here would return the whole corpus.
            return []

        params = {"channel_ids": channels, "limit": query.limit * self._overfetch}
        async with self._engine.connect() as conn:
            for statement in sql.SESSION_SETUP:
                await conn.execute(text(statement))

            lexical = await conn.execute(
                dsql.LEXICAL_DOCUMENT_SEARCH, {**params, "q": query.text}
            )
            lexical_hits = [_hit(r) for r in lexical.mappings()]

            embedded = (await self._embeddings.embed([query.text]))[0]
            vector = await conn.execute(
                dsql.VECTOR_DOCUMENT_SEARCH,
                {**params, "embedding": sql.vector_literal(embedded)},
            )
            vector_hits = [_hit(r) for r in vector.mappings()]

        return fuse_document_hits([lexical_hits, vector_hits], limit=query.limit)


def _hit(row: RowMapping) -> DocumentHit:
    return DocumentHit(
        chunk_id=int(row["id"]),
        document_id=int(row["document_id"]),
        channel=ChannelRef(PLATFORM, int(row["channel_id"])),
        text=str(row["text"]),
        location=str(row["location"]),
        score=float(row["score"]),
        title=cast("str | None", row["title"]),
        heading=cast("str | None", row["heading"]),
        entry_message_id=cast("int | None", row["message_id"]),
        source_url=cast("str | None", row["source_url"]),
    )


if TYPE_CHECKING:  # pragma: no cover - these exist to fail type-checking, not to run
    # Protocols are structural, so nothing else would notice these drifting
    # apart from the ports. These two lines make mypy notice.
    def _store_conforms(store: PostgresDocumentStore) -> DocumentStore:
        return store

    def _search_conforms(search: DocumentSearch) -> DocumentSearchBackend:
        return search
