"""Ingestion: live capture, resumable backfill, and window maintenance.

Platform-agnostic. The Discord specifics live behind `ChatSource`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import structlog

from chatmemory.app.windowing import WindowBuilder
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.messages import Message
from chatmemory.ports.sources import ChatSource, EmbeddingClient
from chatmemory.ports.store import Store

log = structlog.get_logger()


class DocumentSink(Protocol):
    """The document corpus's view of a deletion.

    Documents live in their own tables, so tombstoning a message does not
    withdraw the attachment it carried. Without this the message disappears
    while its uploaded PDF stays searchable.
    """

    async def handle_message_deleted(self, message_id: int, at: datetime | None = None) -> int: ...


@dataclass(frozen=True, slots=True)
class BackfillReport:
    channel: ChannelRef
    imported: int
    complete: bool


class IngestService:
    def __init__(
        self,
        source: ChatSource,
        store: Store,
        windows: WindowBuilder,
        indexed_channels: frozenset[int],
        page_size: int = 100,
        documents: DocumentSink | None = None,
    ) -> None:
        self._source = source
        self._store = store
        self._windows = windows
        self._indexed = indexed_channels
        self._page_size = page_size
        self._documents = documents

    def is_indexed(self, channel: ChannelRef) -> bool:
        return channel.platform_channel_id in self._indexed

    async def capture(self, message: Message) -> bool:
        """Persist one live message. Returns whether it was in scope."""
        if not self.is_indexed(message.channel):
            return False
        await self._store.upsert_messages([message])
        return True

    async def handle_edit(self, message: Message) -> None:
        if self.is_indexed(message.channel):
            # Same upsert path: the revision key makes it idempotent, and
            # content changing is exactly what the conflict clause updates.
            await self._store.upsert_messages([message])

    async def handle_delete(self, platform_message_id: int, at: datetime | None = None) -> None:
        when = at or datetime.now(UTC)
        await self._store.tombstone_message(platform_message_id, when)
        if self._documents is not None:
            # A deleted message must take its attachments with it. These live
            # in separate tables, so tombstoning the message alone leaves the
            # uploaded document fully searchable.
            withdrawn = await self._documents.handle_message_deleted(platform_message_id, when)
            if withdrawn:
                log.info("ingest.documents_withdrawn", message_id=platform_message_id,
                         count=withdrawn)

    async def backfill_page(self, channel: ChannelRef) -> BackfillReport:
        """Import one page of history, oldest-ward from the cursor.

        Newest-first overall: the cursor walks backwards, so recent history --
        which is what people ask about -- is queryable while older history is
        still importing.
        """
        if not self.is_indexed(channel):
            return BackfillReport(channel, 0, complete=True)

        cursor = await self._store.get_cursor(channel)
        page = await self._source.backfill(channel, cursor, self._page_size)
        if not page:
            return BackfillReport(channel, 0, complete=True)

        written = await self._store.upsert_messages(page)
        oldest = min(m.platform_message_id for m in page)
        await self._store.set_cursor(channel, oldest)

        log.info(
            "backfill.page",
            channel=str(channel),
            fetched=len(page),
            written=written,
            oldest=oldest,
        )
        return BackfillReport(channel, written, complete=len(page) < self._page_size)

    async def backfill_channel(self, channel: ChannelRef, max_pages: int = 1000) -> int:
        total = 0
        for _ in range(max_pages):
            report = await self.backfill_page(channel)
            total += report.imported
            if report.complete:
                break
        return total

    async def rebuild_windows(self, channel: ChannelRef, messages: Sequence[Message]) -> int:
        windows = self._windows.build(channel, messages)
        return await self._store.replace_windows(channel, windows)

    async def purge_unindexed(self, channels: Sequence[ChannelRef]) -> int:
        """Remove content for channels that have left indexing scope."""
        removed = 0
        for channel in channels:
            if not self.is_indexed(channel):
                removed += await self._store.purge_channel(channel)
                log.info("ingest.purged_channel", channel=str(channel))
        return removed


class EmbeddingWorker:
    """Drains the backlog of windows without a current embedding."""

    def __init__(
        self, store: Store, embeddings: EmbeddingClient, batch_size: int = 32
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._batch_size = batch_size

    async def run_once(self) -> int:
        pending = await self._store.windows_missing_embeddings(self._batch_size)
        if not pending:
            return 0
        vectors = await self._embeddings.embed([w.text for w in pending])
        for window, vector in zip(pending, vectors, strict=True):
            if window.window_id is None:
                continue
            await self._store.store_embedding(window.window_id, vector)
        return len(pending)

    async def run_forever(self, idle_seconds: float = 5.0) -> None:
        while True:
            try:
                done = await self.run_once()
            except Exception:  # noqa: BLE001 - a worker must not die on one batch
                log.exception("embedding.batch_failed")
                done = 0
            if done == 0:
                await asyncio.sleep(idle_seconds)
