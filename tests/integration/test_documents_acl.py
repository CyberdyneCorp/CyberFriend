"""What only a real database can prove about document retrieval.

Chiefly the same property the conversation suite proves, in the shape
documents take: the permission predicate is an array overlap on the chunk row,
bound into the same statement as the ranking, and it must neither leak a
document to someone who cannot read any of its channels nor quietly reduce how
many results a restricted viewer gets.

The schema is applied by running the change's own migration, so these tests
exercise the table definitions that will actually be deployed rather than a
hand-written copy that can drift from them.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.documents.store import DocumentSearch, PostgresDocumentStore
from chatmemory.app.documents.model import (
    Document,
    DocumentChunk,
    DocumentEntry,
    DocumentFormat,
    DocumentOrigin,
)
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import SearchQuery

pytestmark = pytest.mark.asyncio

OPEN_CH, PRIVATE_CH, THIRD_CH = 100, 300, 400
T0 = datetime(2026, 9, 13, tzinfo=UTC)
DIMS = 1536
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0003_documents.py"


class FixedEmbeddings:
    """Deterministic vectors: these tests are about the predicate, not the model."""

    def __init__(self, seed: float = 0.05) -> None:
        self._seed = seed

    @property
    def dimensions(self) -> int:
        return DIMS

    async def embed(self, texts: list[str] | tuple[str, ...]) -> list[list[float]]:
        return [[self._seed] * DIMS for _ in texts]


def vector(seed: float) -> str:
    return "[" + ",".join(str(seed) for _ in range(DIMS)) + "]"


def viewer(*channel_ids: int) -> Viewer:
    return Viewer(
        person=PersonRef("discord", 1),
        visible_channels=frozenset(ChannelRef("discord", c) for c in channel_ids),
    )


def _apply_migration(sync_connection: Any) -> None:
    spec = importlib.util.spec_from_file_location("migration_0003", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    context = MigrationContext.configure(sync_connection)
    with Operations.context(context):
        module.upgrade()


@pytest_asyncio.fixture
async def documents(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """A database with the document schema applied and no document rows."""
    async with engine.begin() as conn:
        present = await conn.execute(text("SELECT to_regclass('public.document')"))
        if present.scalar() is None:
            await conn.run_sync(_apply_migration)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE document_fetch, document_chunk, document_entry, document "
                "RESTART IDENTITY CASCADE"
            )
        )
        for cid, name in ((OPEN_CH, "general"), (PRIVATE_CH, "leadership"), (THIRD_CH, "ops")):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT (id) DO NOTHING"
                ),
                {"id": cid, "n": name},
            )
    yield engine


def document(identity: str, title: str = "spec") -> Document:
    return Document(
        content_hash=identity,
        origin=DocumentOrigin.ATTACHMENT,
        media_type=DocumentFormat.MARKDOWN,
        byte_size=100,
        title=title,
        fetched_at=T0,
    )


def entry(channel_id: int, message_id: int) -> DocumentEntry:
    return DocumentEntry(
        channel=ChannelRef("discord", channel_id),
        message_id=message_id,
        entered_at=T0,
        uploader=PersonRef("discord", 7),
    )


def chunks(*texts: str) -> list[DocumentChunk]:
    return [
        DocumentChunk(ordinal=i, text=t, location=f"p. {i + 1}") for i, t in enumerate(texts)
    ]


async def embed_all(engine: AsyncEngine, seed: float = 0.05) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE document_chunk SET embedding = CAST(:e AS vector)"),
            {"e": vector(seed)},
        )


# --- visibility ---------------------------------------------------------


async def test_a_document_is_unreachable_to_a_viewer_who_reads_neither_channel(
    documents: AsyncEngine,
) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("private-doc"))
    await store.replace_chunks(stored.document_id, chunks("the rollout is on Thursday"))
    await store.record_entry(stored.document_id, entry(PRIVATE_CH, 1))
    await embed_all(documents)

    search = DocumentSearch(documents, FixedEmbeddings())
    query = SearchQuery(text="rollout")

    assert await search.search_documents(viewer(PRIVATE_CH), query)
    assert await search.search_documents(viewer(OPEN_CH), query) == []
    assert await search.search_documents(viewer(THIRD_CH, OPEN_CH), query) == []
    assert await search.search_documents(viewer(), query) == []


async def test_a_document_shared_into_two_channels_is_readable_through_either(
    documents: AsyncEngine,
) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("shared-doc"))
    await store.replace_chunks(stored.document_id, chunks("the rollout is on Thursday"))
    await store.record_entry(stored.document_id, entry(OPEN_CH, 1))
    await store.record_entry(stored.document_id, entry(PRIVATE_CH, 2))
    await embed_all(documents)

    search = DocumentSearch(documents, FixedEmbeddings())
    query = SearchQuery(text="rollout")

    assert await search.search_documents(viewer(OPEN_CH), query)
    assert await search.search_documents(viewer(PRIVATE_CH), query)
    # One document, one set of chunks, one embedding: shared twice, not twice over.
    async with documents.connect() as conn:
        count = await conn.execute(text("SELECT COUNT(*) FROM document_chunk"))
        assert count.scalar() == 1


async def test_a_citation_points_at_a_channel_the_viewer_can_actually_read(
    documents: AsyncEngine,
) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("shared-doc"))
    await store.replace_chunks(stored.document_id, chunks("the rollout is on Thursday"))
    await store.record_entry(stored.document_id, entry(PRIVATE_CH, 1))
    await store.record_entry(stored.document_id, entry(OPEN_CH, 2))
    await embed_all(documents)

    hits = await DocumentSearch(documents, FixedEmbeddings()).search_documents(
        viewer(OPEN_CH), SearchQuery(text="rollout")
    )
    assert hits[0].channel == ChannelRef("discord", OPEN_CH)
    assert hits[0].entry_message_id == 2
    assert hits[0].location == "p. 1"


# --- the failure this predicate exists to avoid -------------------------


async def test_a_restricted_viewer_is_not_quietly_under_served(
    documents: AsyncEngine,
) -> None:
    """Enough rows that an approximate scan has to choose.

    If the channel predicate were applied after ranking, the viewer who can
    read one channel would receive a fraction of what they asked for, with no
    error anywhere -- and worst for the people in the fewest channels.
    """
    store = PostgresDocumentStore(documents)
    for index in range(40):
        for channel_id in (OPEN_CH, PRIVATE_CH):
            stored = await store.upsert_document(document(f"doc-{channel_id}-{index}"))
            await store.replace_chunks(
                stored.document_id, chunks(f"deployment rollout discussion number {index}")
            )
            await store.record_entry(stored.document_id, entry(channel_id, index))
    await embed_all(documents)

    hits = await DocumentSearch(documents, FixedEmbeddings()).search_documents(
        viewer(PRIVATE_CH), SearchQuery(text="deployment rollout", limit=20)
    )

    assert len(hits) == 20
    assert {hit.channel.platform_channel_id for hit in hits} == {PRIVATE_CH}


# --- removal ------------------------------------------------------------


async def test_deleting_the_introducing_message_withdraws_only_that_share(
    documents: AsyncEngine,
) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("shared-doc"))
    await store.replace_chunks(stored.document_id, chunks("the rollout is on Thursday"))
    await store.record_entry(stored.document_id, entry(OPEN_CH, 1))
    await store.record_entry(stored.document_id, entry(PRIVATE_CH, 2))
    await embed_all(documents)

    search = DocumentSearch(documents, FixedEmbeddings())
    await store.tombstone_entries_for_message(1, datetime.now(UTC))

    assert await search.search_documents(viewer(OPEN_CH), SearchQuery(text="rollout")) == []
    assert await search.search_documents(viewer(PRIVATE_CH), SearchQuery(text="rollout"))

    await store.tombstone_entries_for_message(2, datetime.now(UTC))
    assert (
        await search.search_documents(
            viewer(OPEN_CH, PRIVATE_CH), SearchQuery(text="rollout")
        )
        == []
    )


async def test_a_tombstoned_document_stops_being_returned_immediately(
    documents: AsyncEngine,
) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("withdrawn-doc"))
    await store.replace_chunks(stored.document_id, chunks("the rollout is on Thursday"))
    await store.record_entry(stored.document_id, entry(OPEN_CH, 1))
    await embed_all(documents)

    await store.tombstone_document(stored.document_id, datetime.now(UTC))

    search = DocumentSearch(documents, FixedEmbeddings())
    assert await search.search_documents(viewer(OPEN_CH), SearchQuery(text="rollout")) == []


async def test_superseded_content_is_never_returned_after_reconciliation(
    documents: AsyncEngine,
) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("external-doc"))
    await store.replace_chunks(stored.document_id, chunks("the rollout is on Thursday"))
    await store.record_entry(stored.document_id, entry(OPEN_CH, 1))
    await embed_all(documents)

    await store.replace_chunks(stored.document_id, chunks("the rollout moved to Monday"))
    await embed_all(documents)

    hits = await DocumentSearch(documents, FixedEmbeddings()).search_documents(
        viewer(OPEN_CH), SearchQuery(text="rollout")
    )
    assert [hit.text for hit in hits] == ["the rollout moved to Monday"]


async def test_a_channel_leaving_scope_takes_its_documents_with_it(
    documents: AsyncEngine,
) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("scoped-doc"))
    await store.replace_chunks(stored.document_id, chunks("the rollout is on Thursday"))
    await store.record_entry(stored.document_id, entry(PRIVATE_CH, 1))
    await embed_all(documents)

    await store.purge_channel_documents(ChannelRef("discord", PRIVATE_CH))

    search = DocumentSearch(documents, FixedEmbeddings())
    assert await search.search_documents(viewer(PRIVATE_CH), SearchQuery(text="rollout")) == []


async def test_opt_out_removes_a_persons_uploads(documents: AsyncEngine) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("uploaded-doc"))
    await store.replace_chunks(stored.document_id, chunks("the rollout is on Thursday"))
    await store.record_entry(stored.document_id, entry(OPEN_CH, 1))
    await embed_all(documents)

    removed = await store.purge_person_documents(PersonRef("discord", 7))

    assert removed == 1
    search = DocumentSearch(documents, FixedEmbeddings())
    assert await search.search_documents(viewer(OPEN_CH), SearchQuery(text="rollout")) == []


async def test_retention_removes_documents_that_entered_before_the_cutoff(
    documents: AsyncEngine,
) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("old-doc"))
    await store.replace_chunks(stored.document_id, chunks("the rollout is on Thursday"))
    await store.record_entry(stored.document_id, entry(OPEN_CH, 1))
    await embed_all(documents)

    assert await store.purge_documents_before(T0 - timedelta(days=1)) == 0
    assert await store.purge_documents_before(T0 + timedelta(days=1)) == 1

    search = DocumentSearch(documents, FixedEmbeddings())
    assert await search.search_documents(viewer(OPEN_CH), SearchQuery(text="rollout")) == []


# --- bookkeeping --------------------------------------------------------


async def test_re_ingesting_the_same_bytes_reports_no_change(documents: AsyncEngine) -> None:
    store = PostgresDocumentStore(documents)
    first = await store.upsert_document(document("stable-doc"))
    second = await store.upsert_document(document("stable-doc"))

    assert first.document_id == second.document_id
    assert first.content_changed
    assert not second.content_changed


async def test_failed_fetches_are_counted_and_successful_ones_are_not(
    documents: AsyncEngine,
) -> None:
    from chatmemory.app.documents.model import FetchRecord

    store = PostgresDocumentStore(documents)
    target = "https://cdn.test/spec.md"
    for attempt in (1, 2):
        await store.record_fetch(
            FetchRecord(
                target=target,
                outcome="unretrievable",
                channel=ChannelRef("discord", OPEN_CH),
                message_id=1,
                at=T0,
                attempts=attempt,
            )
        )

    assert await store.fetch_attempts(target) == 2
    assert await store.fetch_attempts("https://cdn.test/other.md") == 0


async def test_document_cost_is_reported_apart_from_conversation(
    documents: AsyncEngine,
) -> None:
    store = PostgresDocumentStore(documents)
    stored = await store.upsert_document(document("costed-doc"))
    await store.replace_chunks(stored.document_id, chunks("a" * 300, "b" * 300))
    await store.record_entry(stored.document_id, entry(OPEN_CH, 1))

    cost = await store.document_cost()
    assert cost.documents >= 1
    assert cost.chunks >= 2
    assert cost.estimated_tokens >= 200
