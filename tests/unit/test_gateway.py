"""Gateway events, modelled as plain objects.

The event names matter as much as the handling: `on_message_delete` fires only
for messages discord.py still holds in memory, which after a restart is none
of them. Anything the corpus has held long enough to be worth retrieving is
deleted through the *raw* event alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from chatmemory.adapters.discord.gateway import GatewayEventHandler
from chatmemory.adapters.discord.source import RawMessage
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message
from tests.unit.test_source import Author, Chan, raw

INDEXED, UNINDEXED, THREAD = 100, 999, 555
CH = ChannelRef("discord", INDEXED)
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@dataclass
class DeletePayload:
    message_id: int
    channel_id: int = INDEXED


@dataclass
class BulkDeletePayload:
    message_ids: set[int]
    channel_id: int = INDEXED


@dataclass
class FakeSink:
    edited: list[Message] = field(default_factory=list)
    deleted: list[tuple[int, datetime | None]] = field(default_factory=list)

    def is_indexed(self, channel: ChannelRef) -> bool:
        return channel.platform_channel_id == INDEXED

    async def handle_edit(self, message: Message) -> None:
        self.edited.append(message)

    async def handle_delete(
        self, platform_message_id: int, at: datetime | None = None
    ) -> None:
        self.deleted.append((platform_message_id, at))


@dataclass
class FakeFeed:
    published: list[Message] = field(default_factory=list)

    def publish(self, message: Message) -> None:
        self.published.append(message)


@dataclass
class FakeCache:
    invalidated: list[object] = field(default_factory=list)

    def invalidate(self, person: object = None) -> None:
        self.invalidated.append(person)


def build() -> tuple[GatewayEventHandler, FakeSink, FakeFeed, FakeCache]:
    sink, feed, cache = FakeSink(), FakeFeed(), FakeCache()
    handler = GatewayEventHandler(sink, feed, caches=[cache], now=lambda: NOW)
    return handler, sink, feed, cache


# --- live capture ------------------------------------------------------


async def test_message_in_an_indexed_channel_is_published() -> None:
    handler, _, feed, _ = build()
    await handler.on_message(raw(1, content="deploy is green"))
    assert [m.content for m in feed.published] == ["deploy is green"]


async def test_message_outside_indexing_scope_is_never_buffered() -> None:
    handler, _, feed, _ = build()
    await handler.on_message(raw(1, channel=Chan(UNINDEXED)))
    assert feed.published == []


async def test_thread_message_is_published_under_its_parent_channel() -> None:
    """The scope check must see the parent, or threads vanish from the corpus."""
    handler, _, feed, _ = build()
    await handler.on_message(raw(1, channel=Chan(THREAD, parent_id=INDEXED)))
    assert [m.channel for m in feed.published] == [CH]


async def test_our_own_output_is_never_ingested() -> None:
    handler, _, feed, _ = build()
    await handler.on_message(raw(1, author=Author(2, bot=True)))
    assert feed.published == []


# --- edits -------------------------------------------------------------


async def test_edit_replaces_the_stored_content() -> None:
    handler, sink, _, _ = build()
    await handler.on_message_edit(raw(1, content="after", edited_at=NOW))
    assert [m.content for m in sink.edited] == ["after"]


async def test_edit_outside_indexing_scope_is_ignored() -> None:
    handler, sink, _, _ = build()
    await handler.on_message_edit(raw(1, channel=Chan(UNINDEXED)))
    assert sink.edited == []


# --- deletions ---------------------------------------------------------


async def test_cached_delete_is_tombstoned() -> None:
    handler, sink, _, _ = build()
    await handler.on_message_delete(raw(1))
    assert sink.deleted == [(1, NOW)]


async def test_uncached_delete_arrives_only_as_a_raw_event() -> None:
    """The case that matters: an older message, deleted after a restart.

    discord.py never cached it, so no `on_message_delete` is dispatched. If
    this event were not handled, the deleted text would stay retrievable
    indefinitely.
    """
    handler, sink, _, _ = build()
    await handler.on_raw_message_delete(DeletePayload(message_id=42))
    assert sink.deleted == [(42, NOW)]


async def test_a_delete_is_not_gated_on_indexing_scope() -> None:
    """Tombstoning an unstored message is free; missing one is not.

    A raw payload names the thread rather than its parent, so a scope check
    here would reject exactly the deletions that came from threads.
    """
    handler, sink, _, _ = build()
    await handler.on_raw_message_delete(DeletePayload(message_id=7, channel_id=THREAD))
    assert sink.deleted == [(7, NOW)]


async def test_bulk_delete_tombstones_every_message() -> None:
    handler, sink, _, _ = build()
    await handler.on_raw_bulk_delete(BulkDeletePayload(message_ids={1, 2, 3}))
    assert sorted(mid for mid, _ in sink.deleted) == [1, 2, 3]


async def test_both_delete_events_for_one_message_are_harmless() -> None:
    """Discord dispatches the cached and raw events for the same deletion."""
    handler, sink, _, _ = build()
    message: RawMessage = raw(5)
    await handler.on_message_delete(message)
    await handler.on_raw_message_delete(DeletePayload(message_id=5))
    assert {mid for mid, _ in sink.deleted} == {5}


# --- permission invalidation -------------------------------------------


def test_member_update_invalidates_only_that_person() -> None:
    handler, _, _, cache = build()
    handler.on_member_changed(PersonRef("discord", 7))
    assert cache.invalidated == [PersonRef("discord", 7)]


def test_channel_permission_change_invalidates_everyone() -> None:
    handler, _, _, cache = build()
    handler.on_permissions_changed()
    assert cache.invalidated == [None]
