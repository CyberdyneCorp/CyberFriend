"""Discord as a `ChatSource`, plus the pass that repairs what the gateway missed.

Two capture paths, deliberately different in shape:

  ``backfill()``  paginated REST history, newest-first, so recent conversation
                  -- which is what people ask about -- becomes queryable while
                  older history is still importing.
  ``stream()``    live messages handed over by the gateway, buffered so a slow
                  database cannot stall the socket that receives them.

Edits and deletions are not part of `ChatSource`. They are corrections to
messages already captured rather than new conversation, so the gateway handler
routes them straight to the ingest service; see `gateway.py`.

`Reconciler` exists because a gateway event that fires while the process is
down is not replayed: nothing tells us afterwards that a message was edited or
deleted during the outage. The only way to notice is to re-read recent history
and compare it against what we hold.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import discord
import structlog

from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

log = structlog.get_logger()

PLATFORM = "discord"

# Used when the platform signals a rate limit without naming an interval.
DEFAULT_RETRY_SECONDS = 5.0
MAX_RATE_LIMIT_ATTEMPTS = 5

Sleeper = Callable[[float], Awaitable[None]]


# --- the shape of a platform message ------------------------------------
#
# Protocols rather than `discord.Message` so the conversion is testable
# against plain objects. discord.py's types are cast to these at the gateway
# boundary; the protocols exist to keep the core testable, not to re-describe
# discord.py's hierarchy.


class RawUser(Protocol):
    id: int
    bot: bool


class RawReference(Protocol):
    message_id: int | None


class RawChannel(Protocol):
    id: int


class RawMessage(Protocol):
    id: int
    content: str
    created_at: datetime
    edited_at: datetime | None
    author: RawUser
    mentions: Sequence[RawUser]
    channel: RawChannel
    reference: RawReference | None


def channel_of(raw: RawMessage) -> tuple[ChannelRef, int | None]:
    """The channel a message is indexed under, and the thread it sits in.

    A message posted in a thread reports the thread as its channel. It is
    indexed under the thread's *parent* instead, because both indexing scope
    and read permission are defined on the parent -- indexing by thread id
    would silently drop every threaded message from a channel in scope, and
    would make the permission predicate check a channel nobody configured.
    """
    channel = raw.channel
    parent_id = cast("int | None", getattr(channel, "parent_id", None))
    if parent_id is not None:
        return ChannelRef(PLATFORM, parent_id), channel.id
    return ChannelRef(PLATFORM, channel.id), None


def to_message(raw: RawMessage) -> Message:
    channel, thread_id = channel_of(raw)
    reference = raw.reference
    return Message(
        platform_message_id=raw.id,
        channel=channel,
        author=PersonRef(PLATFORM, raw.author.id),
        content=raw.content,
        created_at=raw.created_at,
        edited_at=raw.edited_at,
        reply_to_id=reference.message_id if reference is not None else None,
        thread_id=thread_id,
        # Captured as structure rather than left in the text: "what did people
        # ask me?" must not depend on scanning message bodies for a mention.
        mentions=frozenset(PersonRef(PLATFORM, u.id) for u in raw.mentions),
    )


def is_ingestable(raw: RawMessage) -> bool:
    """Bots are never ingested, ourselves least of all.

    Our own answers quote the corpus; ingesting them feeds retrieval its own
    output, which compounds every time someone asks a similar question.
    """
    return not raw.author.bot


def retry_after(error: BaseException) -> float | None:
    """The interval the platform asked us to wait, or None if this is not a 429.

    Duck-typed instead of matched against discord.py's exception classes:
    `RateLimited` carries `retry_after`, an HTTP error carries `status`, and
    the fakes the source is tested against carry neither base class.

    discord.py absorbs most 429s itself by sleeping inside its HTTP client, so
    reaching here means the wait exceeded its own ceiling. Honouring it is
    still cheaper than failing the page.
    """
    status = getattr(error, "status", None)
    explicit = getattr(error, "retry_after", None)
    if status != 429 and explicit is None:
        return None
    return float(explicit) if explicit is not None else DEFAULT_RETRY_SECONDS


# --- history reading ----------------------------------------------------


class HistoryReader(Protocol):
    """One page of raw platform history, newest first."""

    async def fetch(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[RawMessage]: ...


class HistoryChannel(Protocol):
    def history(
        self, *, limit: int, before: object | None, oldest_first: bool
    ) -> AsyncIterator[RawMessage]: ...


ChannelProvider = Callable[[int], HistoryChannel | None]
"""Resolves a channel id against the live gateway cache.

A provider rather than a channel because the source is constructed before the
client connects; None means "not reachable yet", never "empty".
"""


class DiscordHistoryReader:
    def __init__(self, channels: ChannelProvider) -> None:
        self._channels = channels

    async def fetch(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[RawMessage]:
        target = self._channels(channel.platform_channel_id)
        if target is None:
            # Not cached yet, or the bot lost access. Either way this is not
            # the end of the channel's history, so report nothing rather than
            # letting the caller record it as complete.
            log.warning("backfill.channel_unavailable", channel=str(channel))
            return []

        before = discord.Object(id=before_message_id) if before_message_id else None
        # Ordering is stated explicitly: discord.py's default flips depending
        # on which bound is supplied, and a page that arrives oldest-first
        # walks the cursor the wrong way.
        pages = target.history(limit=limit, before=before, oldest_first=False)
        return [raw async for raw in pages]


class DiscordChatSource:
    """Implements `ChatSource` over the Discord gateway and REST history."""

    def __init__(
        self,
        reader: HistoryReader,
        sleep: Sleeper = asyncio.sleep,
        max_attempts: int = MAX_RATE_LIMIT_ATTEMPTS,
    ) -> None:
        self._reader = reader
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._live: asyncio.Queue[Message] = asyncio.Queue()

    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]:
        attempt = 0
        while True:
            attempt += 1
            try:
                page = await self._reader.fetch(channel, before_message_id, limit)
            except Exception as error:
                wait = retry_after(error)
                if wait is None or attempt >= self._max_attempts:
                    raise
                # The cursor is advanced by the caller only after a page is
                # returned, so waiting here costs time and never position.
                log.warning(
                    "backfill.rate_limited",
                    channel=str(channel),
                    retry_after=wait,
                    attempt=attempt,
                )
                await self._sleep(wait)
                continue
            return [to_message(raw) for raw in page if is_ingestable(raw)]

    def publish(self, message: Message) -> None:
        """Hand a live message to the stream. Called from the gateway handler."""
        self._live.put_nowait(message)

    @property
    def pending(self) -> int:
        """Live messages captured but not yet persisted; reported as health."""
        return self._live.qsize()

    async def stream(self) -> AsyncIterator[Message]:
        while True:
            yield await self._live.get()


# --- reconciliation -----------------------------------------------------


class RevisionLedger(Protocol):
    """The slice of the corpus reconciliation compares against.

    Separate from `Store` on purpose: this is the only read path in the
    system that is *not* viewer-scoped, and it returns revisions rather than
    content, so it can never become a way to read messages unfiltered.
    """

    async def stored_revisions(
        self, channel: ChannelRef, since: datetime
    ) -> Mapping[int, datetime]:
        """Message id -> revision, for messages stored since `since`."""
        ...


class CorrectionSink(Protocol):
    """Where reconciliation reports what changed while we were not looking."""

    async def handle_edit(self, message: Message) -> None: ...

    async def handle_delete(
        self, platform_message_id: int, at: datetime | None = None
    ) -> None: ...


class HistorySource(Protocol):
    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]: ...


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    channel: ChannelRef
    scanned: int
    updated: int
    deleted: int


class Reconciler:
    """Re-reads recent history and repairs what the gateway could not tell us.

    An edit or deletion that happens while the process is down is never
    replayed on reconnect: the event is simply lost. Absence from a re-read of
    live history is therefore the only evidence a message was deleted, and a
    changed revision the only evidence it was edited.
    """

    def __init__(
        self,
        source: HistorySource,
        ledger: RevisionLedger,
        sink: CorrectionSink,
        page_size: int = 100,
        max_pages: int = 20,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._source = source
        self._ledger = ledger
        self._sink = sink
        self._page_size = page_size
        self._max_pages = max_pages
        self._now = now

    async def reconcile(self, channel: ChannelRef, since: datetime) -> ReconcileReport:
        live = await self._read_since(channel, since)
        stored = await self._ledger.stored_revisions(channel, since)

        updated = 0
        for message in live.values():
            # Covers both an edit we missed and a message posted during the
            # outage: in each case what we hold differs from what is there.
            if stored.get(message.platform_message_id) != message.revision:
                await self._sink.handle_edit(message)
                updated += 1

        deleted = 0
        at = self._now()
        for message_id in stored:
            if message_id not in live:
                await self._sink.handle_delete(message_id, at)
                deleted += 1

        if updated or deleted:
            log.info(
                "reconcile.repaired",
                channel=str(channel),
                updated=updated,
                deleted=deleted,
            )
        return ReconcileReport(channel, len(live), updated, deleted)

    async def _read_since(
        self, channel: ChannelRef, since: datetime
    ) -> dict[int, Message]:
        """Live messages newer than `since`, walking back a page at a time."""
        seen: dict[int, Message] = {}
        before: int | None = None
        for _ in range(self._max_pages):
            page = await self._source.backfill(channel, before, self._page_size)
            if not page:
                break
            for message in page:
                if message.created_at >= since:
                    seen[message.platform_message_id] = message
            oldest = min(page, key=lambda m: m.platform_message_id)
            if oldest.created_at < since or len(page) < self._page_size:
                break
            before = oldest.platform_message_id
        return seen


def reconcile_window(lookback: timedelta, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - lookback
