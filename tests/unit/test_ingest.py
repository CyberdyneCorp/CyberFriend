"""Ingestion: scope, idempotence, resumability, tombstones."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

from chatmemory.app.ingest import EmbeddingWorker, IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message, Window

INDEXED, UNINDEXED = 100, 999
CH = ChannelRef("discord", INDEXED)
OUT = ChannelRef("discord", UNINDEXED)
T0 = datetime(2026, 9, 13, tzinfo=UTC)


def msg(mid: int, channel: ChannelRef = CH, minutes: int = 0, content: str = "hi") -> Message:
    return Message(
        platform_message_id=mid,
        channel=channel,
        author=PersonRef("discord", 1),
        content=content,
        created_at=T0 + timedelta(minutes=minutes),
    )


class FakeStore:
    """Models the store's identity rules: upsert by id, revision decides writes."""

    def __init__(self) -> None:
        self.messages: dict[int, Message] = {}
        self.cursors: dict[int, int] = {}
        self.tombstoned: list[int] = []
        self.purged: list[ChannelRef] = []
        self.windows: dict[int, list[Window]] = {}
        self.embeddings: dict[int, Sequence[float]] = {}
        self.pending_windows: list[Window] = []
        self.dirty: dict[ChannelRef, datetime] = {}

    async def upsert_messages(self, messages: Sequence[Message]) -> int:
        written = 0
        for m in messages:
            existing = self.messages.get(m.platform_message_id)
            if existing is None or existing.revision != m.revision:
                self.messages[m.platform_message_id] = m
                written += 1
        return written

    async def tombstone_message(self, platform_message_id: int, at: datetime) -> None:
        self.tombstoned.append(platform_message_id)

    async def purge_channel(self, channel: ChannelRef) -> int:
        self.purged.append(channel)
        return 1

    async def resolve_person(self, person: PersonRef, display_name: str) -> int:
        return person.platform_user_id

    async def get_cursor(self, channel: ChannelRef) -> int | None:
        return self.cursors.get(channel.platform_channel_id)

    async def set_cursor(self, channel: ChannelRef, oldest_message_id: int) -> None:
        current = self.cursors.get(channel.platform_channel_id)
        self.cursors[channel.platform_channel_id] = (
            oldest_message_id if current is None else min(current, oldest_message_id)
        )

    async def messages_without_window(self, limit: int) -> Sequence[Message]:
        return []

    async def replace_windows(self, channel: ChannelRef, windows: Sequence[Window]) -> int:
        self.windows[channel.platform_channel_id] = list(windows)
        return len(windows)

    async def windows_missing_embeddings(self, limit: int) -> Sequence[Window]:
        batch = self.pending_windows[:limit]
        self.pending_windows = self.pending_windows[limit:]
        return batch

    async def store_embedding(self, window_id: int, embedding: Sequence[float]) -> None:
        self.embeddings[window_id] = embedding

    async def mark_windows_dirty(self, channel: ChannelRef, at: datetime) -> None:
        current = self.dirty.get(channel)
        self.dirty[channel] = at if current is None else min(current, at)

    async def dirty_channels(self, limit: int = 20) -> Sequence[tuple[ChannelRef, datetime]]:
        return sorted(self.dirty.items(), key=lambda kv: kv[1])[:limit]

    async def clear_windows_dirty(self, channel: ChannelRef, up_to: datetime) -> None:
        at = self.dirty.get(channel)
        if at is not None and at <= up_to:
            del self.dirty[channel]


class FakeSource:
    """History newest-first, paginated backwards from a cursor."""

    def __init__(self, history: list[Message]) -> None:
        self.history = sorted(history, key=lambda m: -m.platform_message_id)
        self.calls = 0

    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]:
        self.calls += 1
        candidates = [
            m
            for m in self.history
            if m.channel == channel
            and (before_message_id is None or m.platform_message_id < before_message_id)
        ]
        return candidates[:limit]

    def stream(self) -> AsyncIterator[Message]:  # pragma: no cover - unused here
        raise NotImplementedError


def build(
    history: list[Message] | None = None, page_size: int = 3
) -> tuple[IngestService, FakeStore, FakeSource]:
    store, source = FakeStore(), FakeSource(history or [])
    service = IngestService(source, store, WindowBuilder(), frozenset({INDEXED}), page_size)
    return service, store, source


# --- scope -------------------------------------------------------------


async def test_message_in_scope_is_stored() -> None:
    service, store, _ = build()
    assert await service.capture(msg(1))
    assert 1 in store.messages


async def test_message_out_of_scope_is_not_stored() -> None:
    """Indexing is opt-in; out-of-scope content must never be persisted."""
    service, store, _ = build()
    assert not await service.capture(msg(1, channel=OUT))
    assert store.messages == {}


async def test_channel_leaving_scope_is_purged() -> None:
    service, store, _ = build()
    await service.purge_unindexed([CH, OUT])
    assert store.purged == [OUT]


# --- idempotence and resumability --------------------------------------


async def test_ingesting_the_same_message_twice_writes_once() -> None:
    service, store, _ = build()
    await service.capture(msg(1))
    await service.capture(msg(1))
    assert len(store.messages) == 1


async def test_edit_updates_content() -> None:
    service, store, _ = build()
    await service.capture(msg(1, content="before"))
    edited = Message(
        platform_message_id=1,
        channel=CH,
        author=PersonRef("discord", 1),
        content="after",
        created_at=T0,
        edited_at=T0 + timedelta(minutes=1),
    )
    await service.handle_edit(edited)
    assert store.messages[1].content == "after"


async def test_backfill_imports_all_history_without_duplicates() -> None:
    history = [msg(i, minutes=i) for i in range(1, 11)]
    service, store, _ = build(history)
    imported = await service.backfill_channel(CH)
    assert imported == 10
    assert len(store.messages) == 10


async def test_backfill_resumes_from_its_cursor_without_gaps() -> None:
    """Interrupt mid-channel, restart, and the corpus is still exact."""
    history = [msg(i, minutes=i) for i in range(1, 11)]
    service, store, source = build(history)

    await service.backfill_page(CH)  # one page only, then "crash"
    partial = len(store.messages)
    assert 0 < partial < 10

    resumed = await service.backfill_channel(CH)
    assert len(store.messages) == 10
    assert resumed + partial == 10  # no message written twice


async def test_backfill_walks_backwards_so_recent_history_lands_first() -> None:
    history = [msg(i, minutes=i) for i in range(1, 11)]
    service, store, _ = build(history, page_size=3)
    await service.backfill_page(CH)
    assert set(store.messages) == {8, 9, 10}


async def test_backfill_skips_unindexed_channels() -> None:
    service, store, source = build([msg(1, channel=OUT)])
    report = await service.backfill_page(OUT)
    assert report.complete and report.imported == 0
    assert source.calls == 0


# --- deletion ----------------------------------------------------------


async def test_delete_tombstones_the_message() -> None:
    service, store, _ = build()
    await service.capture(msg(1))
    await service.handle_delete(1)
    assert store.tombstoned == [1]


# --- embeddings --------------------------------------------------------


class FakeEmbeddings:
    dimensions = 3

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]


async def test_embedding_worker_drains_the_backlog() -> None:
    store, embeddings = FakeStore(), FakeEmbeddings()
    store.pending_windows = [
        Window(CH, (1,), "text", T0, T0, window_id=i) for i in range(1, 4)
    ]
    worker = EmbeddingWorker(store, embeddings, batch_size=2)  # type: ignore[arg-type]

    assert await worker.run_once() == 2
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    assert set(store.embeddings) == {1, 2, 3}
