"""The ingest process's jobs, driven without a gateway or a database.

Covers the two places where the process itself (rather than the service it
calls) can lose data: the live loop dropping capture after one failure, and
window rebuilds carrying deleted text back into retrieval.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from chatmemory.app.ingest import IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message, Window
from chatmemory.entrypoints.ingest import (
    indexed_channels,
    live_loop,
    rebuild_pending_windows,
)
from chatmemory.health import HealthState

INDEXED, OTHER = 100, 200
CH, CH2 = ChannelRef("discord", INDEXED), ChannelRef("discord", OTHER)
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
AUTHOR = PersonRef("discord", 7)


def msg(
    mid: int,
    content: str,
    minutes: int = 0,
    channel: ChannelRef = CH,
    deleted: bool = False,
) -> Message:
    return Message(
        platform_message_id=mid,
        channel=channel,
        author=AUTHOR,
        content=content,
        created_at=T0 + timedelta(minutes=minutes),
        deleted_at=T0 + timedelta(hours=1) if deleted else None,
    )


class FakeStore:
    def __init__(self, pending: Sequence[Message] = ()) -> None:
        self.pending = list(pending)
        self.messages: dict[int, Message] = {}
        self.windows: dict[ChannelRef, list[Window]] = {}
        self.fail_next = False
        self.dirty: dict[ChannelRef, datetime] = {}

    async def upsert_messages(self, messages: Sequence[Message]) -> int:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("database went away")
        for m in messages:
            self.messages[m.platform_message_id] = m
        return len(messages)

    async def messages_without_window(self, limit: int) -> Sequence[Message]:
        batch, self.pending = self.pending[:limit], self.pending[limit:]
        return batch

    async def replace_windows(self, channel: ChannelRef, windows: Sequence[Window]) -> int:
        self.windows[channel] = list(windows)
        return len(windows)

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
    """Only the live half of the source: a queue the loop drains."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Message] = asyncio.Queue()

    def publish(self, message: Message) -> None:
        self._queue.put_nowait(message)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def stream(self):  # type: ignore[no-untyped-def]
        while True:
            yield await self._queue.get()


def service_for(store: FakeStore) -> IngestService:
    return IngestService(
        source=FakeSource(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        windows=WindowBuilder(),
        indexed_channels=frozenset({INDEXED, OTHER}),
    )


# --- window rebuilds ---------------------------------------------------


async def test_a_rebuilt_window_excludes_a_deleted_message() -> None:
    """Tombstoning the message is not enough: its text lives in the window too.

    A window is a concatenation of message text. Rebuilding it without the
    deleted message is what actually makes the retracted words unreachable
    through semantic retrieval.
    """
    store = FakeStore(
        [
            msg(1, "standup moved to 10", minutes=0),
            msg(2, "wrong channel, ignore", minutes=1, deleted=True),
            msg(3, "see you then", minutes=2),
        ]
    )

    rebuilt = await rebuild_pending_windows(service_for(store), store)  # type: ignore[arg-type]

    assert rebuilt == 1
    text = "\n".join(w.text for w in store.windows[CH])
    assert "wrong channel" not in text
    assert "standup moved to 10" in text
    assert store.windows[CH][0].message_ids == (1, 3)


async def test_rebuilds_are_grouped_per_channel() -> None:
    """Windows never span channels: the permission predicate is per channel."""
    store = FakeStore([msg(1, "a"), msg(2, "b", channel=CH2)])

    await rebuild_pending_windows(service_for(store), store)  # type: ignore[arg-type]

    assert set(store.windows) == {CH, CH2}


async def test_nothing_pending_is_not_work() -> None:
    store = FakeStore([])
    assert await rebuild_pending_windows(service_for(store), store) == 0  # type: ignore[arg-type]
    assert store.windows == {}


# --- the live loop -----------------------------------------------------


async def drain(source: FakeSource, predicate, tries: int = 200) -> None:  # type: ignore[no-untyped-def]
    for _ in range(tries):
        await asyncio.sleep(0)
        if predicate():
            return


async def test_live_messages_are_persisted_as_they_arrive() -> None:
    store = FakeStore()
    source, state = FakeSource(), HealthState()
    task = asyncio.create_task(live_loop(source, service_for(store), state))  # type: ignore[arg-type]

    source.publish(msg(1, "deploy is green"))
    await drain(source, lambda: 1 in store.messages)
    task.cancel()

    assert store.messages[1].content == "deploy is green"
    assert state.last_message_ingested_at is not None


async def test_one_failed_write_does_not_end_live_capture() -> None:
    """A stalled database must cost a message, not the rest of the day's traffic."""
    store = FakeStore()
    store.fail_next = True
    source, state = FakeSource(), HealthState()
    task = asyncio.create_task(live_loop(source, service_for(store), state))  # type: ignore[arg-type]

    source.publish(msg(1, "lost to the outage"))
    source.publish(msg(2, "captured anyway"))
    await drain(source, lambda: 2 in store.messages)
    task.cancel()

    assert 1 not in store.messages
    assert store.messages[2].content == "captured anyway"


# --- scope -------------------------------------------------------------


def test_indexed_channels_are_read_from_configuration() -> None:
    class FakeSettings:
        indexed_channel_ids = frozenset({OTHER, INDEXED})

    assert indexed_channels(FakeSettings()) == [CH, CH2]  # type: ignore[arg-type]
