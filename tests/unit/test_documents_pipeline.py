"""Ingestion, removal, scope and reconciliation, against an in-memory store.

The fake store models the rules that matter rather than the SQL: identity by
content hash, entries as independent shares, and visibility as the union of
the live entries' channels. Getting those wrong in the fake would make these
tests pass for the wrong reason, so the visibility rule is implemented here
the same way the schema implements it -- as a set on the chunk.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from chatmemory.adapters.documents import parsers
from chatmemory.app.documents.chunking import ProseChunker
from chatmemory.app.documents.model import (
    CandidateAttachment,
    Document,
    DocumentChunk,
    DocumentCost,
    DocumentEntry,
    DocumentFormat,
    FetchRecord,
    ParseOutcome,
    SkipReason,
    StoredDocument,
)
from chatmemory.app.documents.pipeline import DocumentIngestService, content_hash
from chatmemory.app.documents.policy import DocumentPolicy, ExternalPolicy, ExternalSource
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from tests.unit.test_documents_corpus import (
    TEST_LIMITS,
    TEST_POLICY,
    docx_bomb,
    malformed_pdf,
    markdown,
    pdf,
)

pytestmark = pytest.mark.asyncio

OPEN, PRIVATE, UNINDEXED = 100, 300, 999
INDEXED = frozenset({OPEN, PRIVATE})
T0 = datetime(2026, 9, 13, tzinfo=UTC)
UPLOADER = PersonRef("discord", 7)

DRIVE = ExternalSource(
    name="drive",
    hosts=frozenset({"drive.google.com"}),
    scopes=frozenset({"https://www.googleapis.com/auth/drive.file"}),
    shared_containers=frozenset({"team-drive"}),
)


def channel(cid: int) -> ChannelRef:
    return ChannelRef("discord", cid)


def message(mid: int = 1, cid: int = OPEN, content: str = "here it is") -> Message:
    return Message(
        platform_message_id=mid,
        channel=channel(cid),
        author=UPLOADER,
        content=content,
        created_at=T0,
    )


def attachment(
    data_url: str = "https://cdn.test/spec.md",
    filename: str = "spec.md",
    mid: int = 1,
    cid: int = OPEN,
    aid: int = 500,
) -> CandidateAttachment:
    return CandidateAttachment(
        attachment_id=aid,
        filename=filename,
        declared_media_type="text/markdown",
        url=data_url,
        byte_size=1024,
        channel=channel(cid),
        message_id=mid,
        uploader=UPLOADER,
    )


def viewer(*channel_ids: int) -> Viewer:
    return Viewer(
        person=PersonRef("discord", 1),
        visible_channels=frozenset(channel(c) for c in channel_ids),
    )


SPEC = markdown([("Rollout", "We deploy on Thursday, behind a flag.")])
OTHER = markdown([("Budget", "The number is forty two thousand.")])


# --- fakes -------------------------------------------------------------


@dataclass
class StoredRow:
    document: Document
    chunks: list[DocumentChunk] = field(default_factory=list)
    entries: list[DocumentEntry] = field(default_factory=list)
    withdrawn: set[tuple[int, str]] = field(default_factory=set)
    deleted: bool = False

    @property
    def live_entries(self) -> list[DocumentEntry]:
        return [
            e for e in self.entries if (e.message_id, e.source_url or "") not in self.withdrawn
        ]

    @property
    def channels(self) -> set[int]:
        """Exactly what the schema denormalises onto each chunk."""
        if self.deleted:
            return set()
        return {e.channel.platform_channel_id for e in self.live_entries}


class FakeDocumentStore:
    def __init__(self) -> None:
        self.rows: dict[str, StoredRow] = {}
        self.by_id: dict[int, StoredRow] = {}
        self.fetches: list[FetchRecord] = []
        self._next_id = 1

    async def upsert_document(self, document: Document) -> StoredDocument:
        existing = self.rows.get(document.identity)
        if existing is None:
            row = StoredRow(document=document)
            document_id = self._next_id
            self._next_id += 1
            row.document = replace(document, document_id=document_id)
            self.rows[document.identity] = row
            self.by_id[document_id] = row
            return StoredDocument(document_id, content_changed=True)

        changed = existing.document.content_hash != document.content_hash
        document_id = existing.document.document_id or 0
        existing.document = replace(document, document_id=document_id)
        return StoredDocument(document_id, content_changed=changed)

    async def document_content_hash(self, identity: str) -> str | None:
        row = self.rows.get(identity)
        if row is None or row.deleted:
            return None
        return row.document.content_hash

    async def record_entry(self, document_id: int, entry: DocumentEntry) -> None:
        row = self.by_id[document_id]
        row.withdrawn.discard((entry.message_id, entry.source_url or ""))
        row.entries.append(entry)

    async def replace_chunks(self, document_id: int, chunks: Sequence[DocumentChunk]) -> int:
        self.by_id[document_id].chunks = list(chunks)
        return len(chunks)

    async def tombstone_document(self, document_id: int, at: datetime) -> None:
        self.by_id[document_id].deleted = True

    async def tombstone_entries_for_message(self, message_id: int, at: datetime) -> int:
        withdrawn = 0
        for row in self.by_id.values():
            for entry in row.live_entries:
                if entry.message_id == message_id:
                    row.withdrawn.add((entry.message_id, entry.source_url or ""))
                    withdrawn += 1
        return withdrawn

    async def purge_channel_documents(self, target: ChannelRef) -> int:
        purged = 0
        for row in self.by_id.values():
            for entry in list(row.live_entries):
                if entry.channel == target:
                    row.withdrawn.add((entry.message_id, entry.source_url or ""))
                    purged += 1
        return purged

    async def purge_person_documents(self, person: PersonRef) -> int:
        purged = 0
        for row in self.by_id.values():
            for entry in list(row.live_entries):
                if entry.uploader == person:
                    row.withdrawn.add((entry.message_id, entry.source_url or ""))
                    purged += 1
        return purged

    async def purge_documents_before(self, cutoff: datetime) -> int:
        purged = 0
        for row in self.by_id.values():
            for entry in list(row.live_entries):
                if entry.entered_at < cutoff:
                    row.withdrawn.add((entry.message_id, entry.source_url or ""))
                    purged += 1
        return purged

    async def record_fetch(self, record: FetchRecord) -> None:
        self.fetches.append(record)

    async def fetch_attempts(self, target: str) -> int:
        failures = [
            f.attempts for f in self.fetches if f.target == target and f.outcome != "fetched"
        ]
        return max(failures) if failures else 0

    async def chunks_missing_embeddings(self, limit: int) -> Sequence[tuple[int, str]]:
        return []

    async def store_chunk_embedding(self, chunk_id: int, embedding: Sequence[float]) -> None:
        return None

    async def external_documents_due(self, limit: int) -> Sequence[Document]:
        return [
            row.document
            for row in self.by_id.values()
            if row.document.external_uri and not row.deleted
        ][:limit]

    async def document_cost(self) -> DocumentCost:
        chunks = [c for row in self.by_id.values() if not row.deleted for c in row.chunks]
        return DocumentCost(
            documents=len([r for r in self.by_id.values() if not r.deleted]),
            chunks=len(chunks),
            estimated_tokens=sum(len(c.text) for c in chunks) // 3,
        )

    # --- what a viewer can actually retrieve ---------------------------

    def visible_text(self, who: Viewer) -> list[str]:
        """Models the schema's predicate: overlap of entry channels."""
        permitted = {c.platform_channel_id for c in who.visible_channels}
        return [
            chunk.text
            for row in self.by_id.values()
            if row.channels & permitted
            for chunk in row.chunks
        ]


class FakeFetcher:
    def __init__(self, responses: dict[str, bytes | None]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def fetch(self, url: str, max_bytes: int, timeout: float) -> bytes | None:
        self.calls.append(url)
        return self.responses.get(url)


class InProcessParser:
    """The real parsers, without the process spawn. Same outcomes, faster."""

    def __init__(self) -> None:
        self.calls = 0

    async def parse(self, data: bytes, fmt: DocumentFormat, filename: str) -> ParseOutcome:
        self.calls += 1
        return parsers.parse(data, fmt, TEST_LIMITS)


@dataclass
class FakeExternal:
    data: bytes
    filename: str = "spec.md"
    media_type: str = "text/markdown"
    revision: str | None = "v1"
    containers: frozenset[str] = frozenset({"team-drive"})


class FakeDriveSource:
    name = "drive"

    def __init__(self, result: FakeExternal | None, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.calls: list[str] = []

    async def fetch(self, url: str, max_bytes: int, timeout: float) -> FakeExternal | None:
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("drive is down")
        return self.result


def build(
    store: FakeDocumentStore,
    fetcher: FakeFetcher,
    policy: DocumentPolicy | None = None,
    sources: Sequence[object] = (),
    parser: InProcessParser | None = None,
) -> DocumentIngestService:
    return DocumentIngestService(
        store=store,
        parser=parser or InProcessParser(),
        chunker=ProseChunker(TEST_POLICY.chunking),
        fetcher=fetcher,
        policy=policy or TEST_POLICY,
        indexed_channels=INDEXED,
        external_sources=sources,  # type: ignore[arg-type]
    )


# --- attachment capture -------------------------------------------------


async def test_an_attachment_is_retrieved_at_ingest_and_becomes_retrievable() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({"https://cdn.test/spec.md": SPEC})
    report = await build(store, fetcher).capture_attachments(message(), [attachment()])

    assert report.indexed == 1
    assert "Thursday" in " ".join(store.visible_text(viewer(OPEN)))
    # Fetched now, not linked: Discord's CDN URLs expire.
    assert fetcher.calls == ["https://cdn.test/spec.md"]


async def test_an_attachment_outside_indexing_scope_is_never_retrieved() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({"https://cdn.test/spec.md": SPEC})
    report = await build(store, fetcher).capture_attachments(
        message(cid=UNINDEXED), [attachment(cid=UNINDEXED)]
    )

    assert report.indexed == 0
    assert fetcher.calls == []
    assert report.skipped[0][1] is SkipReason.OUT_OF_SCOPE


async def test_a_failed_retrieval_is_recorded_and_not_retried_forever() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({"https://cdn.test/spec.md": None})
    service = build(store, fetcher)

    for _ in range(TEST_POLICY.max_attachment_attempts + 3):
        await service.capture_attachments(message(), [attachment()])

    assert len(fetcher.calls) == TEST_POLICY.max_attachment_attempts
    assert all(f.outcome == "unretrievable" for f in store.fetches)


async def test_one_hostile_file_does_not_stop_the_next_one() -> None:
    store = FakeDocumentStore()
    fetcher = FakeFetcher(
        {
            "https://cdn.test/bomb.docx": docx_bomb(),
            "https://cdn.test/broken.pdf": malformed_pdf(),
            "https://cdn.test/spec.md": SPEC,
        }
    )
    report = await build(store, fetcher).capture_attachments(
        message(),
        [
            attachment("https://cdn.test/bomb.docx", "bomb.docx", aid=1),
            attachment("https://cdn.test/broken.pdf", "broken.pdf", aid=2),
            attachment("https://cdn.test/spec.md", "spec.md", aid=3),
        ],
    )

    assert report.indexed == 1
    assert report.skipped_count == 2
    assert "Thursday" in " ".join(store.visible_text(viewer(OPEN)))


async def test_a_pdf_keeps_its_page_locations_through_to_the_chunks() -> None:
    """A citation has to say where in the document to look, not just which one."""
    store = FakeDocumentStore()
    long_first_page = "\n".join(f"Line {i} of the opening summary." for i in range(20))
    data = pdf([long_first_page, "the detail is on page two"])
    fetcher = FakeFetcher({"https://cdn.test/report.pdf": data})
    await build(store, fetcher).capture_attachments(
        message(),
        [
            CandidateAttachment(
                attachment_id=1,
                filename="report.pdf",
                declared_media_type="application/pdf",
                url="https://cdn.test/report.pdf",
                byte_size=len(data),
                channel=channel(OPEN),
                message_id=1,
                uploader=UPLOADER,
            )
        ],
    )

    chunks = [c for row in store.by_id.values() for c in row.chunks]
    assert len(chunks) > 1
    # Every chunk says which page it starts on, and the last page's text is
    # indexed rather than dropped off the end.
    assert all(c.location.startswith("p. ") for c in chunks)
    assert chunks[0].location == "p. 1 line 1"
    assert any("page two" in c.text for c in chunks)


# --- visibility ---------------------------------------------------------


async def test_a_document_is_unreachable_to_a_viewer_who_reads_neither_channel() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({"https://cdn.test/spec.md": SPEC})
    await build(store, fetcher).capture_attachments(
        message(cid=PRIVATE), [attachment(cid=PRIVATE)]
    )

    assert store.visible_text(viewer(PRIVATE))
    assert store.visible_text(viewer(OPEN)) == []
    assert store.visible_text(viewer()) == []


async def test_the_same_document_in_two_channels_is_one_document_readable_via_either() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({"https://cdn.test/spec.md": SPEC})
    service = build(store, fetcher)

    await service.capture_attachments(message(mid=1, cid=OPEN), [attachment(mid=1, cid=OPEN)])
    await service.capture_attachments(
        message(mid=2, cid=PRIVATE), [attachment(mid=2, cid=PRIVATE)]
    )

    assert len(store.by_id) == 1  # embedded once, shared twice
    assert store.visible_text(viewer(OPEN))
    assert store.visible_text(viewer(PRIVATE))


async def test_deleting_the_introducing_message_withdraws_only_its_share() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({"https://cdn.test/spec.md": SPEC})
    service = build(store, fetcher)
    await service.capture_attachments(message(mid=1, cid=OPEN), [attachment(mid=1, cid=OPEN)])
    await service.capture_attachments(
        message(mid=2, cid=PRIVATE), [attachment(mid=2, cid=PRIVATE)]
    )

    await service.handle_message_deleted(1)

    assert store.visible_text(viewer(OPEN)) == []
    assert store.visible_text(viewer(PRIVATE))

    await service.handle_message_deleted(2)
    assert store.visible_text(viewer(OPEN, PRIVATE)) == []


async def test_a_channel_leaving_scope_takes_its_documents_with_it() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({"https://cdn.test/spec.md": SPEC})
    await build(store, fetcher).capture_attachments(
        message(cid=PRIVATE), [attachment(cid=PRIVATE)]
    )

    narrowed = DocumentIngestService(
        store=store,
        parser=InProcessParser(),
        chunker=ProseChunker(TEST_POLICY.chunking),
        fetcher=fetcher,
        policy=TEST_POLICY,
        indexed_channels=frozenset({OPEN}),
    )
    await narrowed.purge_unindexed([channel(OPEN), channel(PRIVATE)])

    assert store.visible_text(viewer(PRIVATE)) == []


# --- external documents -------------------------------------------------


def external_policy(**overrides: object) -> DocumentPolicy:
    settings: dict[str, object] = {"enabled": True, "sources": (DRIVE,)}
    settings.update(overrides)
    return DocumentPolicy(
        limits=TEST_LIMITS,
        chunking=TEST_POLICY.chunking,
        external=ExternalPolicy(**settings),  # type: ignore[arg-type]
    )


async def test_a_configured_link_is_fetched_and_indexed() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({})
    source = FakeDriveSource(FakeExternal(SPEC))
    linked = message(content="the spec https://drive.google.com/d/abc123")

    report = await build(store, fetcher, external_policy(), [source]).capture_links(linked)

    assert report.indexed == 1
    assert source.calls == ["https://drive.google.com/d/abc123"]
    assert "Thursday" in " ".join(store.visible_text(viewer(OPEN)))


async def test_nothing_is_fetched_when_external_access_is_disabled() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({})
    source = FakeDriveSource(FakeExternal(SPEC))
    linked = message(content="https://drive.google.com/d/abc123")

    report = await build(
        store, fetcher, external_policy(enabled=False), [source]
    ).capture_links(linked)

    assert report.indexed == 0
    assert source.calls == []


async def test_a_document_the_team_does_not_share_is_not_disclosed() -> None:
    """The escalation this rule exists to stop.

    The credential can read it; the team cannot. The link was posted by
    someone with no access to the target, and the answer must be the same as
    for a document that does not exist -- indexed nowhere, and recorded
    without confirming it is there.
    """
    store, fetcher = FakeDocumentStore(), FakeFetcher({})
    private_to_someone_else = FakeExternal(
        b"# Salaries\nEverything you should not see.",
        containers=frozenset({"someone-elses-folder"}),
    )
    source = FakeDriveSource(private_to_someone_else)
    linked = message(content="https://drive.google.com/d/secret")

    report = await build(store, fetcher, external_policy(), [source]).capture_links(linked)

    assert report.indexed == 0
    assert report.skipped[0][1] is SkipReason.UNRETRIEVABLE
    assert store.visible_text(viewer(OPEN, PRIVATE)) == []
    # The audit row says a fetch failed. It does not say the document exists.
    assert [f.outcome for f in store.fetches] == ["unretrievable"]
    assert all(f.document_hash is None for f in store.fetches)


async def test_every_fetch_is_recorded_with_what_where_and_which_message() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({})
    source = FakeDriveSource(FakeExternal(SPEC))
    linked = message(mid=77, content="https://drive.google.com/d/abc123")

    await build(store, fetcher, external_policy(), [source]).capture_links(linked)

    record = store.fetches[-1]
    assert record.target == "https://drive.google.com/d/abc123"
    assert record.channel == channel(OPEN)
    assert record.message_id == 77
    assert record.outcome == "fetched"


async def test_an_unavailable_external_system_does_not_stop_attachment_ingestion() -> None:
    store = FakeDocumentStore()
    fetcher = FakeFetcher({"https://cdn.test/spec.md": SPEC})
    source = FakeDriveSource(None, fail=True)
    linked = message(content="see https://drive.google.com/d/abc123")

    service = build(store, fetcher, external_policy(), [source])
    report = await service.capture_message(linked, [attachment()])

    assert report.indexed == 1  # the attachment still went in
    assert "Thursday" in " ".join(store.visible_text(viewer(OPEN)))


async def test_an_external_link_carries_the_channel_it_was_posted_in() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({})
    source = FakeDriveSource(FakeExternal(SPEC))
    linked = message(cid=PRIVATE, content="https://drive.google.com/d/abc123")

    await build(store, fetcher, external_policy(), [source]).capture_links(linked)

    # Visibility is the Discord channel's, whatever the document's own ACL says.
    assert store.visible_text(viewer(PRIVATE))
    assert store.visible_text(viewer(OPEN)) == []


# --- reconciliation -----------------------------------------------------


async def test_changed_source_content_is_re_chunked_and_supersedes_the_old() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({})
    source = FakeDriveSource(FakeExternal(SPEC))
    service = build(store, fetcher, external_policy(), [source])
    await service.capture_links(message(content="https://drive.google.com/d/abc123"))

    source.result = FakeExternal(OTHER, revision="v2")
    report = await service.reconcile_external()

    assert report.indexed == 1
    text = " ".join(store.visible_text(viewer(OPEN)))
    assert "forty two thousand" in text
    assert "Thursday" not in text


async def test_unchanged_content_is_not_parsed_or_embedded_again() -> None:
    """One document can cost more to embed than a month of conversation."""
    store, fetcher = FakeDocumentStore(), FakeFetcher({})
    parser = InProcessParser()
    source = FakeDriveSource(FakeExternal(SPEC))
    service = build(store, fetcher, external_policy(), [source], parser=parser)
    await service.capture_links(message(content="https://drive.google.com/d/abc123"))
    assert parser.calls == 1

    await service.reconcile_external()
    assert parser.calls == 1


async def test_losing_access_at_the_source_is_treated_as_deletion() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({})
    source = FakeDriveSource(FakeExternal(SPEC))
    service = build(store, fetcher, external_policy(), [source])
    await service.capture_links(message(content="https://drive.google.com/d/abc123"))

    source.result = None  # revoked, deleted, or refused: all the same here
    report = await service.reconcile_external()

    assert report.skipped[0][1] is SkipReason.UNRETRIEVABLE
    assert store.visible_text(viewer(OPEN, PRIVATE)) == []


async def test_a_document_moved_out_of_the_shared_scope_stops_being_returned() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({})
    source = FakeDriveSource(FakeExternal(SPEC))
    service = build(store, fetcher, external_policy(), [source])
    await service.capture_links(message(content="https://drive.google.com/d/abc123"))

    source.result = FakeExternal(SPEC, containers=frozenset({"private-folder"}))
    await service.reconcile_external()

    assert store.visible_text(viewer(OPEN, PRIVATE)) == []


# --- governance ---------------------------------------------------------


async def test_retention_and_opt_out_cover_documents_not_only_messages() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({"https://cdn.test/spec.md": SPEC})
    await build(store, fetcher).capture_attachments(message(), [attachment()])

    assert await store.purge_documents_before(T0 - timedelta(days=1)) == 0
    assert store.visible_text(viewer(OPEN))

    assert await store.purge_person_documents(UPLOADER) == 1
    assert store.visible_text(viewer(OPEN)) == []


async def test_document_embedding_cost_is_reported_separately() -> None:
    store, fetcher = FakeDocumentStore(), FakeFetcher({"https://cdn.test/spec.md": SPEC})
    await build(store, fetcher).capture_attachments(message(), [attachment()])

    cost = await store.document_cost()
    assert cost.documents == 1
    assert cost.chunks >= 1
    assert cost.estimated_tokens > 0


async def test_identical_bytes_are_one_document() -> None:
    assert content_hash(SPEC) == content_hash(bytes(SPEC))
    assert content_hash(SPEC) != content_hash(OTHER)
