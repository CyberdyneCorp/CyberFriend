"""The Discord source: conversion, pagination, rate limits, live buffering.

Every fake here is a plain object. Nothing in this file talks to Discord, and
nothing in the code under test needs it to.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from chatmemory.adapters.discord.source import (
    DEFAULT_RETRY_SECONDS,
    DiscordChatSource,
    RawMessage,
    is_ingestable,
    to_message,
)
from chatmemory.app.ingest import IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

CHANNEL, THREAD, OTHER = 100, 555, 999
CH = ChannelRef("discord", CHANNEL)
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


# --- plain stand-ins for discord.py objects ----------------------------


@dataclass
class Author:
    id: int
    bot: bool = False


@dataclass
class Ref:
    message_id: int | None


@dataclass
class Chan:
    id: int
    parent_id: int | None = None


@dataclass
class Raw:
    id: int
    content: str = "hello"
    created_at: datetime = T0
    author: Author = field(default_factory=lambda: Author(7))
    channel: Chan = field(default_factory=lambda: Chan(CHANNEL))
    edited_at: datetime | None = None
    mentions: list[Author] = field(default_factory=list)
    reference: Ref | None = None


def raw(mid: int, **kwargs: object) -> RawMessage:
    return Raw(mid, **kwargs)  # type: ignore[arg-type]


# --- conversion --------------------------------------------------------


def test_conversion_keeps_author_channel_and_timestamps() -> None:
    message = to_message(raw(1, created_at=T0, edited_at=T0 + timedelta(minutes=1)))
    assert message.platform_message_id == 1
    assert message.channel == CH
    assert message.author == PersonRef("discord", 7)
    assert message.created_at == T0
    assert message.revision == T0 + timedelta(minutes=1)


def test_mentions_are_captured_as_structure() -> None:
    """"What did people ask me?" must not depend on scanning message text."""
    message = to_message(raw(1, mentions=[Author(11), Author(12)]))
    assert message.mentions == frozenset(
        {PersonRef("discord", 11), PersonRef("discord", 12)}
    )


def test_reply_records_the_message_it_answers() -> None:
    message = to_message(raw(1, reference=Ref(message_id=42)))
    assert message.reply_to_id == 42


def test_thread_message_is_indexed_under_its_parent_channel() -> None:
    """Indexing scope and read permission are both defined on the parent.

    Indexing by thread id would drop every threaded message from a channel
    that is in scope, and point the permission predicate at a channel nobody
    configured.
    """
    message = to_message(raw(1, channel=Chan(THREAD, parent_id=CHANNEL)))
    assert message.channel == CH
    assert message.thread_id == THREAD


# A member setting their email in a channel: the bot promises to show an email
# only in its owner's DMs, so the message itself must never reach the corpus,
# or retrieval and citations repeat it to the room and to other people's DMs.
@pytest.mark.parametrize(
    "content",
    [
        "<@999> my email is leo@x.com",
        "<@!999> please remember my e-mail address is leo@x.com",
        "hey <@999>, use leo@x.com as my email",
        "<@999> meu email é leo@x.com",
        "<@999> my email is not-an-email",
        "my email is leo@x.com",
    ],
)
def test_a_message_stating_ones_own_email_is_never_indexed(content: str) -> None:
    message = raw(1, content=content, mentions=[Author(999, bot=True)])
    assert to_message(message) is None
    assert not is_ingestable(message)


@pytest.mark.parametrize(
    "content",
    [
        "<@999> call me Leo",
        "<@999> what is the email of Leo",
        "the email relay is down again",
        "<@999> why did my email bounce?",
    ],
)
def test_other_messages_about_facts_or_email_are_still_indexed(content: str) -> None:
    message = raw(1, content=content, mentions=[Author(999, bot=True)])
    converted = to_message(message)
    assert converted is not None and converted.content == content
    assert is_ingestable(message)


def test_bot_messages_are_not_ingestable() -> None:
    assert not is_ingestable(raw(1, author=Author(7, bot=True)))
    assert is_ingestable(raw(2))


# --- history reading ---------------------------------------------------


class FakeRateLimit(Exception):
    """Shaped like the platform's 429, sharing none of its base classes."""

    def __init__(self, retry_after: float | None = None, status: int = 429) -> None:
        super().__init__("rate limited")
        self.status = status
        if retry_after is not None:
            self.retry_after = retry_after


class FakeReader:
    """Newest-first history, optionally failing the first N fetches."""

    def __init__(self, messages: list[RawMessage], failures: list[Exception] | None = None):
        self.messages = sorted(messages, key=lambda m: -m.id)
        self.failures = failures or []
        self.fetches = 0

    async def fetch(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[RawMessage]:
        self.fetches += 1
        if self.failures:
            raise self.failures.pop(0)
        candidates = [
            m
            for m in self.messages
            if before_message_id is None or m.id < before_message_id
        ]
        return candidates[:limit]


class Clock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def source(reader: FakeReader, clock: Clock | None = None) -> DiscordChatSource:
    return DiscordChatSource(reader, sleep=(clock or Clock()).sleep)


async def test_backfill_returns_a_page_newest_first() -> None:
    reader = FakeReader([raw(i) for i in range(1, 11)])
    page = await source(reader).backfill(CH, None, 3)
    assert [m.platform_message_id for m in page] == [10, 9, 8]


async def test_backfill_pages_backwards_from_the_cursor() -> None:
    reader = FakeReader([raw(i) for i in range(1, 11)])
    page = await source(reader).backfill(CH, 8, 3)
    assert [m.platform_message_id for m in page] == [7, 6, 5]


async def test_backfill_drops_bot_messages() -> None:
    reader = FakeReader([raw(1), raw(2, author=Author(9, bot=True))])
    page = await source(reader).backfill(CH, None, 10)
    assert [m.platform_message_id for m in page] == [1]


# --- rate limits -------------------------------------------------------


async def test_rate_limit_waits_the_interval_the_platform_named() -> None:
    clock = Clock()
    reader = FakeReader([raw(1)], failures=[FakeRateLimit(retry_after=7.5)])
    page = await source(reader, clock).backfill(CH, None, 10)

    assert clock.slept == [7.5]
    assert [m.platform_message_id for m in page] == [1]
    assert reader.fetches == 2  # the same page, retried


async def test_rate_limit_without_an_interval_falls_back_to_a_default() -> None:
    clock = Clock()
    reader = FakeReader([raw(1)], failures=[FakeRateLimit()])
    await source(reader, clock).backfill(CH, None, 10)
    assert clock.slept == [DEFAULT_RETRY_SECONDS]


async def test_errors_that_are_not_rate_limits_are_not_retried() -> None:
    clock = Clock()
    reader = FakeReader([raw(1)], failures=[RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        await source(reader, clock).backfill(CH, None, 10)
    assert clock.slept == []


class CursorStore:
    """Only the cursor half of the store; backfill position is what is at stake."""

    def __init__(self) -> None:
        self.cursors: dict[int, int] = {}
        self.messages: dict[int, Message] = {}

    async def upsert_messages(self, messages: Sequence[Message]) -> int:
        for m in messages:
            self.messages[m.platform_message_id] = m
        return len(messages)

    async def get_cursor(self, channel: ChannelRef) -> int | None:
        return self.cursors.get(channel.platform_channel_id)

    async def set_cursor(self, channel: ChannelRef, oldest_message_id: int) -> None:
        self.cursors[channel.platform_channel_id] = oldest_message_id


async def test_a_rate_limit_that_outlasts_our_patience_keeps_its_place() -> None:
    """Giving up must cost a page, never the position in the channel."""
    clock = Clock()
    reader = FakeReader([raw(i) for i in range(1, 11)], failures=[])
    chat = DiscordChatSource(reader, sleep=clock.sleep, max_attempts=2)
    store = CursorStore()
    service = IngestService(chat, store, WindowBuilder(), frozenset({CHANNEL}), 3)  # type: ignore[arg-type]

    await service.backfill_page(CH)
    assert store.cursors[CHANNEL] == 8

    reader.failures = [FakeRateLimit(retry_after=1.0), FakeRateLimit(retry_after=1.0)]
    with pytest.raises(FakeRateLimit):
        await service.backfill_page(CH)

    assert store.cursors[CHANNEL] == 8  # unmoved, so the next attempt resumes here
    assert clock.slept == [1.0]  # waited once, then ran out of attempts


# --- the live feed -----------------------------------------------------


def live(mid: int) -> Message:
    return Message(mid, CH, PersonRef("discord", 7), "hi", T0)


async def test_published_messages_arrive_on_the_stream_in_order() -> None:
    chat = source(FakeReader([]))
    chat.publish(live(1))
    chat.publish(live(2))

    stream = chat.stream()
    assert (await anext(stream)).platform_message_id == 1
    assert (await anext(stream)).platform_message_id == 2


async def test_pending_reports_what_has_not_been_persisted_yet() -> None:
    """Buffer depth is health: a stalled store shows up here before anywhere else."""
    chat = source(FakeReader([]))
    assert chat.pending == 0
    chat.publish(live(1))
    assert chat.pending == 1
