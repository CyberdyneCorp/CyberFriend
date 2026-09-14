"""Document ingestion: capture, parse, chunk, index, reconcile.

Two rules shape every method here.

The first is that a bad file is an ordinary input. Attachments are uploaded by
anyone in the server, so an archive bomb, a malformed PDF or a file whose name
lies about its contents is a Tuesday, not an incident. Nothing on this path
raises for those: they are counted as skips and ingestion continues. The only
exceptions that escape are the ones that mean the process itself is broken.

The second is that visibility comes from Discord. A document is readable
because someone shared it into a channel, and `DocumentEntry` is the record of
that act. Nothing here consults an external system's permissions, which is why
there is exactly one permission model in the corpus rather than three.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog

from chatmemory.app.documents.chunking import ProseChunker
from chatmemory.app.documents.links import ExternalLink, links_from_message
from chatmemory.app.documents.model import (
    CandidateAttachment,
    Document,
    DocumentEntry,
    DocumentOrigin,
    FetchRecord,
    IngestReport,
    SkipReason,
)
from chatmemory.app.documents.policy import DocumentPolicy
from chatmemory.app.documents.ports import (
    ContentFetcher,
    DocumentParser,
    DocumentStore,
    ExternalDocumentSource,
    FetchedExternal,
)
from chatmemory.app.documents.sniffing import classify
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.messages import Message
from chatmemory.ports.sources import EmbeddingClient

log = structlog.get_logger()

# The single sentence a user ever sees about a document we could not read.
# It is the same whether the document is missing, private, or refused: saying
# which would confirm that a document exists, which is the disclosure the
# bounded-credential rule exists to prevent.
UNRETRIEVABLE = "That document could not be retrieved."

FETCHED = "fetched"
UNRETRIEVABLE_OUTCOME = "unretrievable"
SKIPPED = "skipped"


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DocumentIngestService:
    """Turns attachments and configured-source links into retrievable prose."""

    def __init__(
        self,
        store: DocumentStore,
        parser: DocumentParser,
        chunker: ProseChunker,
        fetcher: ContentFetcher,
        policy: DocumentPolicy,
        indexed_channels: frozenset[int],
        external_sources: Sequence[ExternalDocumentSource] = (),
    ) -> None:
        # Checked at construction, not at first fetch: an over-scoped
        # credential must stop the deployment rather than quietly read
        # something it should not have, months later.
        if policy.external.enabled:
            policy.external.verify_credentials()
        self._store = store
        self._parser = parser
        self._chunker = chunker
        self._fetcher = fetcher
        self._policy = policy
        self._indexed = indexed_channels
        self._sources = {s.name: s for s in external_sources}

    def is_indexed(self, channel: ChannelRef) -> bool:
        return channel.platform_channel_id in self._indexed

    # --- attachments ---------------------------------------------------

    async def capture_message(
        self, message: Message, attachments: Sequence[CandidateAttachment] = ()
    ) -> IngestReport:
        """Everything one message brings with it: its files and its links."""
        files = await self.capture_attachments(message, attachments)
        links = await self.capture_links(message)
        return IngestReport(
            indexed=files.indexed + links.indexed,
            skipped=files.skipped + links.skipped,
            fetch_failures=files.fetch_failures + links.fetch_failures,
        )

    async def capture_attachments(
        self, message: Message, attachments: Sequence[CandidateAttachment]
    ) -> IngestReport:
        if not self.is_indexed(message.channel) or not message.is_visible:
            # Out of scope means never retrieved, not retrieved and discarded.
            return IngestReport(
                skipped=tuple((a.filename, SkipReason.OUT_OF_SCOPE) for a in attachments)
            )

        indexed = 0
        skipped: list[tuple[str, SkipReason]] = []
        failures: list[str] = []
        for attachment in attachments:
            reason = await self._ingest_attachment(attachment)
            if reason is None:
                indexed += 1
                continue
            skipped.append((attachment.filename, reason))
            if reason is SkipReason.FETCH_FAILED:
                failures.append(attachment.url)
        return IngestReport(indexed, tuple(skipped), tuple(failures))

    async def _ingest_attachment(self, attachment: CandidateAttachment) -> SkipReason | None:
        limits = self._policy.limits
        if attachment.byte_size > limits.max_input_bytes:
            # Declared size, so it only saves a download; the real limit is
            # enforced against the bytes that actually arrive.
            await self._record(attachment.url, SKIPPED, attachment)
            return SkipReason.TOO_LARGE

        attempts = await self._store.fetch_attempts(attachment.url)
        if attempts >= self._policy.max_attachment_attempts:
            # Bounded retries: a permanently broken CDN link must not be
            # retried on every reconciliation pass forever.
            return SkipReason.FETCH_FAILED

        data = await self._fetch_bytes(attachment.url, limits.max_input_bytes)
        if data is None:
            await self._record(attachment.url, UNRETRIEVABLE_OUTCOME, attachment, attempts + 1)
            return SkipReason.FETCH_FAILED

        entry = DocumentEntry(
            channel=attachment.channel,
            message_id=attachment.message_id,
            entered_at=datetime.now(UTC),
            uploader=attachment.uploader,
            attachment_id=attachment.attachment_id,
            source_url=attachment.url,
        )
        reason = await self._ingest_bytes(
            data,
            filename=attachment.filename,
            media_type=attachment.declared_media_type,
            origin=DocumentOrigin.ATTACHMENT,
            entry=entry,
        )
        await self._record(
            attachment.url, SKIPPED if reason else FETCHED, attachment, hash_of=data
        )
        return reason

    async def _fetch_bytes(self, url: str, max_bytes: int) -> bytes | None:
        try:
            return await self._fetcher.fetch(url, max_bytes, self._policy.limits.max_seconds)
        except Exception:  # noqa: BLE001 - one unreachable file, not a failed ingest
            log.warning("document.fetch_failed", url=url)
            return None

    # --- external links ------------------------------------------------

    async def capture_links(self, message: Message) -> IngestReport:
        """Follow the configured-source links in a message. Nothing else.

        When external fetching is off this returns immediately: the switch is
        checked before any link is even looked at, so "disabled" means no
        outbound request of any kind rather than a request that is discarded.
        """
        if not self._policy.external.enabled or not self.is_indexed(message.channel):
            return IngestReport()

        indexed = 0
        skipped: list[tuple[str, SkipReason]] = []
        failures: list[str] = []
        for link in links_from_message(message, self._policy):
            reason = await self._ingest_link(link)
            if reason is None:
                indexed += 1
                continue
            skipped.append((link.url, reason))
            if reason is SkipReason.UNRETRIEVABLE:
                failures.append(link.url)
        return IngestReport(indexed, tuple(skipped), tuple(failures))

    async def _ingest_link(self, link: ExternalLink) -> SkipReason | None:
        source = self._sources.get(link.source.name)
        if source is None:
            return SkipReason.SOURCE_NOT_CONFIGURED

        attempts = await self._store.fetch_attempts(link.url)
        if attempts >= self._policy.external.max_attempts:
            return SkipReason.UNRETRIEVABLE

        fetched = await self._fetch_external(source, link.url)
        if fetched is None:
            await self._record_link(link, UNRETRIEVABLE_OUTCOME, attempts + 1)
            return SkipReason.UNRETRIEVABLE

        if not link.source.permits_container(fetched.containers):
            # Readable by our credential but not shared with the team. Treated
            # exactly like a document that does not exist, because saying
            # otherwise is the escalation: post a link, learn a secret.
            await self._record_link(link, UNRETRIEVABLE_OUTCOME, attempts + 1)
            return SkipReason.UNRETRIEVABLE

        entry = DocumentEntry(
            channel=link.message.channel,
            message_id=link.message.platform_message_id,
            entered_at=datetime.now(UTC),
            uploader=link.message.author,
            source_url=link.url,
        )
        reason = await self._ingest_bytes(
            fetched.data,
            filename=fetched.filename,
            media_type=fetched.media_type,
            origin=DocumentOrigin.EXTERNAL,
            entry=entry,
            external_uri=link.url,
            revision=fetched.revision,
        )
        await self._record_link(link, SKIPPED if reason else FETCHED)
        return reason

    async def _fetch_external(
        self, source: ExternalDocumentSource, url: str
    ) -> FetchedExternal | None:
        """One bounded, read-only fetch. Never raises into the caller.

        An external system being down is not a reason for message ingestion to
        stop, so everything it can throw stops here.
        """
        external = self._policy.external
        try:
            return await source.fetch(url, external.max_fetch_bytes, external.max_fetch_seconds)
        except Exception:  # noqa: BLE001 - an unavailable system must not stop ingestion
            log.warning("document.external_unavailable", source=source.name, url=url)
            return None

    # --- shared path ---------------------------------------------------

    async def _ingest_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        media_type: str | None,
        origin: DocumentOrigin,
        entry: DocumentEntry | None,
        external_uri: str | None = None,
        revision: str | None = None,
    ) -> SkipReason | None:
        """Classify, parse, chunk and store one document's bytes."""
        verdict = classify(
            data, filename, media_type, self._policy.allowed_formats, self._policy.limits
        )
        if verdict.fmt is None:
            log.info("document.skipped", filename=filename, reason=verdict.skipped)
            return verdict.skipped

        digest = content_hash(data)
        identity = external_uri or digest
        unchanged = await self._store.document_content_hash(identity) == digest

        title: str | None = None
        metadata: tuple[tuple[str, str], ...] = ()
        parsed = None
        if not unchanged:
            outcome = await self._parser.parse(data, verdict.fmt, filename)
            if outcome.document is None:
                log.info("document.parse_skipped", filename=filename, reason=outcome.skipped)
                return outcome.skipped
            if outcome.document.is_empty:
                return SkipReason.EMPTY
            parsed = outcome.document
            title = parsed.title
            metadata = parsed.metadata

        document = Document(
            content_hash=digest,
            origin=origin,
            media_type=verdict.fmt,
            byte_size=len(data),
            title=title or filename,
            external_uri=external_uri,
            source_revision=revision,
            metadata=metadata,
            fetched_at=datetime.now(UTC),
        )
        stored = await self._store.upsert_document(document)
        if parsed is not None:
            # Content changed (or is new), so the chunks built from the old
            # content are superseded and must not survive the rebuild.
            chunks = self._chunker.chunk(parsed.segments)
            await self._store.replace_chunks(stored.document_id, chunks)
        if entry is not None:
            await self._store.record_entry(stored.document_id, entry)
        return None

    # --- removal and scope ---------------------------------------------

    async def handle_message_deleted(self, message_id: int, at: datetime | None = None) -> int:
        """Withdraw everything a deleted message shared.

        A document that also entered through a message that remains stays
        readable through that one; the store decides that, because it is the
        only place that knows the other entries.
        """
        return await self._store.tombstone_entries_for_message(message_id, at or datetime.now(UTC))

    async def purge_unindexed(self, channels: Sequence[ChannelRef]) -> int:
        removed = 0
        for channel in channels:
            if not self.is_indexed(channel):
                removed += await self._store.purge_channel_documents(channel)
                log.info("document.purged_channel", channel=str(channel))
        return removed

    # --- reconciliation ------------------------------------------------

    async def reconcile_external(self, limit: int = 20) -> IngestReport:
        """Refresh indexed external content, and withdraw what is gone.

        Losing access at the source is treated exactly like deletion. Serving
        content from a document the bot can no longer open is serving from a
        cache that outlived its permission.
        """
        if not self._policy.external.enabled:
            return IngestReport()

        refreshed = 0
        withdrawn: list[tuple[str, SkipReason]] = []
        for document in await self._store.external_documents_due(limit):
            reason = await self._reconcile_one(document)
            if reason is None:
                refreshed += 1
            else:
                withdrawn.append((document.external_uri or "", reason))
        return IngestReport(refreshed, tuple(withdrawn))

    async def _reconcile_one(self, document: Document) -> SkipReason | None:
        uri = document.external_uri
        if uri is None or document.document_id is None:
            return SkipReason.SOURCE_NOT_CONFIGURED

        configured = self._policy.external.source_for(uri)
        source = None if configured is None else self._sources.get(configured.name)
        if configured is None or source is None:
            await self._store.tombstone_document(document.document_id, datetime.now(UTC))
            return SkipReason.SOURCE_NOT_CONFIGURED

        fetched = await self._fetch_external(source, uri)
        # Gone, refused, or no longer shared with the team: all three mean the
        # same thing here, which is stop returning it.
        if fetched is None or not configured.permits_container(fetched.containers):
            await self._store.tombstone_document(document.document_id, datetime.now(UTC))
            return SkipReason.UNRETRIEVABLE

        return await self._ingest_bytes(
            fetched.data,
            filename=fetched.filename,
            media_type=fetched.media_type,
            origin=DocumentOrigin.EXTERNAL,
            entry=None,
            external_uri=uri,
            revision=fetched.revision,
        )

    # --- audit ---------------------------------------------------------

    async def _record(
        self,
        target: str,
        outcome: str,
        attachment: CandidateAttachment,
        attempts: int = 1,
        hash_of: bytes | None = None,
    ) -> None:
        await self._store.record_fetch(
            FetchRecord(
                target=target,
                outcome=outcome,
                channel=attachment.channel,
                message_id=attachment.message_id,
                at=datetime.now(UTC),
                document_hash=content_hash(hash_of) if hash_of is not None else None,
                attempts=attempts,
            )
        )

    async def _record_link(self, link: ExternalLink, outcome: str, attempts: int = 1) -> None:
        await self._store.record_fetch(
            FetchRecord(
                target=link.url,
                outcome=outcome,
                channel=link.message.channel,
                message_id=link.message.platform_message_id,
                at=datetime.now(UTC),
                attempts=attempts,
            )
        )


class DocumentEmbeddingWorker:
    """Drains the backlog of chunks without an embedding.

    Separate from the conversation worker only because the backlog is a
    different table; the model, the dimensions and therefore the vector space
    are the same, which is what lets one query match both kinds.
    """

    def __init__(
        self, store: DocumentStore, embeddings: EmbeddingClient, batch_size: int = 32
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._batch_size = batch_size

    async def run_once(self) -> int:
        pending = await self._store.chunks_missing_embeddings(self._batch_size)
        if not pending:
            return 0
        vectors = await self._embeddings.embed([text for _, text in pending])
        for (chunk_id, _), vector in zip(pending, vectors, strict=True):
            await self._store.store_chunk_embedding(chunk_id, vector)
        return len(pending)
