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

from chatmemory.app.scope import ScopeProvider, as_scope
from chatmemory.app.windowing import WindowBuilder
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.messages import Message
from chatmemory.ports.sources import ChatSource, EmbeddingClient, SourceUnavailable
from chatmemory.ports.store import Store

log = structlog.get_logger()


class DocumentSink(Protocol):
    """The document corpus's view of a deletion.

    Documents live in their own tables, so tombstoning a message does not
    withdraw the attachment it carried. Without this the message disappears
    while its uploaded PDF stays searchable.
    """

    async def handle_message_deleted(self, message_id: int, at: datetime | None = None) -> int: ...


class TraceSink(Protocol):
    """The trace store's view of a deletion.

    Tracing copies retrieved text into a second store that has no tombstones
    of its own. Without this a message deleted from the corpus stays legible
    in every trace that quoted it, which is the guarantee this project makes
    most loudly, broken quietly.
    """

    async def withdraw_message(self, message_id: int) -> None: ...


class DecisionSink(Protocol):
    """The decision log's view of a deletion.

    A decision is a summary of the messages it rests on, kept in its own
    table. Deleting a message tombstones the row, which no cascade sees, so
    without this the retracted words stay restated in the decision.
    """

    async def withdraw_message(self, message_id: int) -> int: ...


@dataclass(frozen=True, slots=True)
class BackfillReport:
    channel: ChannelRef
    imported: int
    complete: bool
    unavailable: bool = False


class IngestService:
    def __init__(
        self,
        source: ChatSource,
        store: Store,
        windows: WindowBuilder,
        indexed_channels: ScopeProvider | frozenset[int],
        page_size: int = 100,
        documents: DocumentSink | None = None,
        traces: TraceSink | None = None,
        decisions: DecisionSink | None = None,
    ) -> None:
        self._source = source
        self._store = store
        self._windows = windows
        # A provider, read on every decision, never a copy. A set captured
        # here is a channel an operator removed still being captured, and one
        # they added never being captured, until somebody restarts ingest.
        self._scope = as_scope(indexed_channels)
        self._page_size = page_size
        self._documents = documents
        self._traces = traces
        self._decisions = decisions

    @property
    def scope(self) -> ScopeProvider:
        return self._scope

    def is_indexed(self, channel: ChannelRef) -> bool:
        return channel.platform_channel_id in self._scope.current()

    async def capture(self, message: Message) -> bool:
        """Persist one live message. Returns whether it was in scope."""
        if not self.is_indexed(message.channel):
            return False
        await self._store.upsert_messages([message])
        await self._mark_dirty(message.channel, message.created_at)
        return True

    async def _mark_dirty(self, channel: ChannelRef, at: datetime) -> None:
        """Schedule the channel's windows to be re-formed from `at`.

        Anything that changes a channel's content marks it, including edits
        and deletions. A trigger keyed on "this message has no window" can
        only fire once per message, so an edited message would keep its
        pre-edit window forever and consecutive messages could never be
        merged into one window.
        """
        await self._store.mark_windows_dirty(channel, at)

    async def handle_edit(self, message: Message) -> None:
        if self.is_indexed(message.channel):
            # Same upsert path: the revision key makes it idempotent, and
            # content changing is exactly what the conflict clause updates.
            await self._store.upsert_messages([message])
            # The window still holds the pre-edit text until it is re-formed.
            await self._mark_dirty(message.channel, message.created_at)

    async def handle_delete(
        self,
        platform_message_id: int,
        at: datetime | None = None,
        channel: ChannelRef | None = None,
        created_at: datetime | None = None,
    ) -> None:
        when = at or datetime.now(UTC)
        # Valid for an id we have never stored: the delete-before-insert case
        # is the store's to make durable, not this layer's to detect.
        await self._store.tombstone_message(platform_message_id, when)
        if channel is not None:
            # The surviving neighbours must be re-formed without the retracted
            # text; tombstoning the window alone would take the whole
            # conversation out of retrieval. The store marks this too, from
            # the message row -- which is what covers the callers that have no
            # channel to give, reconciliation above all. Marking here as well
            # costs nothing: the watermark keeps the earliest of the two.
            await self._mark_dirty(channel, created_at or when)
        if self._documents is not None:
            # A deleted message must take its attachments with it. These live
            # in separate tables, so tombstoning the message alone leaves the
            # uploaded document fully searchable.
            withdrawn = await self._documents.handle_message_deleted(platform_message_id, when)
            if withdrawn:
                log.info("ingest.documents_withdrawn", message_id=platform_message_id,
                         count=withdrawn)
        if self._decisions is not None:
            withdrawn = await self._decisions.withdraw_message(platform_message_id)
            if withdrawn:
                log.info("ingest.decisions_withdrawn", message_id=platform_message_id,
                         count=withdrawn)
        if self._traces is not None:
            # Last, and never able to stop the rest. The tombstone above has
            # already been applied, so a trace store that is down delays the
            # copy being withdrawn without delaying the deletion itself --
            # which is the only ordering a person deleting a message would
            # accept.
            await self._traces.withdraw_message(platform_message_id)

    async def backfill_page(self, channel: ChannelRef) -> BackfillReport:
        """Import one page of history, oldest-ward from the cursor.

        Newest-first overall: the cursor walks backwards, so recent history --
        which is what people ask about -- is queryable while older history is
        still importing.
        """
        if not self.is_indexed(channel):
            return BackfillReport(channel, 0, complete=True)

        cursor = await self._store.get_cursor(channel)
        try:
            page = await self._source.backfill(channel, cursor, self._page_size)
        except SourceUnavailable:
            # Could not be asked, so nothing is known about what remains.
            # Reporting completion here would retire the channel from
            # backfill entirely on the strength of a question never answered.
            return BackfillReport(channel, 0, complete=False, unavailable=True)
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
            if report.complete or report.unavailable:
                # Unavailable stops this sweep without recording progress, so
                # the next one starts from the same cursor and tries again.
                break
        return total

    async def rebuild_windows(self, channel: ChannelRef, messages: Sequence[Message]) -> int:
        windows = self._windows.build(channel, messages)
        return await self._store.replace_windows(channel, windows)

    async def rewindow(self, channel: ChannelRef, since: datetime) -> int:
        """Re-form a channel's windows from `since`, reading and writing atomically.

        The store does the read and the write in one transaction so that a
        deletion arriving in between cannot end up published inside a freshly
        built live window.
        """
        rewinder = getattr(self._store, "rewindow_channel", None)
        if rewinder is None:
            return 0
        count: int = await rewinder(channel, since, self._windows.build)
        return count

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
