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

from chatmemory.app.routing import states_own_contact
from chatmemory.app.voice import declared_type, from_discord_cdn
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.media import AUDIO_TYPES, IMAGE_TYPES, MediaKind, MediaRef
from chatmemory.domain.messages import Message
from chatmemory.ports.sources import SourceUnavailable

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


class RawAttachment(Protocol):
    id: int
    filename: str
    size: int
    url: str
    content_type: str | None
    duration: float | None


class RawMessage(Protocol):
    id: int
    content: str
    created_at: datetime
    edited_at: datetime | None
    author: RawUser
    mentions: Sequence[RawUser]
    channel: RawChannel
    reference: RawReference | None


# Discord channel type ids for threads. A private thread is readable only by
# members explicitly added to it, regardless of who can read its parent.
PRIVATE_THREAD_TYPE = 12


def is_private_thread(channel: RawChannel) -> bool:
    """Whether this channel's membership is narrower than its parent's.

    Checked three ways because discord.py exposes it differently depending on
    the object: `Thread.is_private()`, a `type` enum with value 12, or an
    `invitable`/`locked` pair on partials. Any signal of privacy counts --
    the cost of a false positive is one un-indexed thread; the cost of a
    false negative is disclosing a private conversation to everyone who can
    read the parent channel.
    """
    is_private = getattr(channel, "is_private", None)
    if callable(is_private):
        try:
            if bool(is_private()):
                return True
        except Exception:  # noqa: BLE001 - a partial object may not support it
            return True
    channel_type = getattr(channel, "type", None)
    type_value = getattr(channel_type, "value", channel_type)
    return type_value == PRIVATE_THREAD_TYPE


def channel_of(raw: RawMessage) -> tuple[ChannelRef | None, int | None]:
    """The channel a message is indexed under, and the thread it sits in.

    A message posted in a *public* thread is indexed under the thread's
    parent, because scope and read permission are both defined there -- and
    indexing by thread id would silently drop every threaded message from a
    channel that is in scope.

    A message in a *private* thread returns None and is not indexed at all.
    Its readers are the people added to that thread, which is a strictly
    narrower set than the parent's readers, so filing it under the parent
    would make it retrievable by everyone who can read the parent. Indexing
    it under its own id is not a fix either: the permission resolver derives
    visibility from channel overwrites, which do not describe thread
    membership, so it would still resolve to the parent's audience.
    """
    channel = raw.channel
    parent_id = cast("int | None", getattr(channel, "parent_id", None))
    if parent_id is not None:
        if is_private_thread(channel):
            return None, channel.id
        return ChannelRef(PLATFORM, parent_id), channel.id
    return ChannelRef(PLATFORM, channel.id), None


def _display_name(author: RawUser) -> str:
    """The best name the platform gives us for this author.

    Tried in the order a reader would recognise: the chosen display name,
    then the per-server nickname, then the account name. Falls back to the
    empty string rather than the numeric id, so a caller can tell "unknown"
    from "called 713763086305329162".
    """
    for attribute in ("global_name", "display_name", "name"):
        value = getattr(author, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def withholds_personal_fact(raw: RawMessage) -> bool:
    """A message giving the author's own email, phone, address or birth date,
    which is never indexed.

    Checked here, in the one conversion every path shares -- live messages,
    edits and history backfill -- so no path can store what another withholds.
    """
    return states_own_contact(raw.content)


def _media_kind(content_type: str, voice: bool) -> MediaKind | None:
    if content_type in AUDIO_TYPES:
        return MediaKind.VOICE if voice else MediaKind.AUDIO
    if content_type in IMAGE_TYPES:
        return MediaKind.IMAGE
    return None


def media_of(raw: RawMessage) -> tuple[MediaRef, ...]:
    """The attachments worth keeping a pending media row for.

    Only an allowlisted declared type served from the Discord CDN: a URL
    anywhere else is a host the process would later be pointed at by somebody
    else's payload. A voice note is told apart by the message's
    IS_VOICE_MESSAGE flag, which the client sets on a recording and never on an
    uploaded file. Read with getattr, as `channel_of` reads threads, because
    the plain objects the core is tested against need not carry either.
    """
    voice = bool(getattr(getattr(raw, "flags", None), "voice", False))
    attachments = cast("Sequence[RawAttachment]", getattr(raw, "attachments", ()))
    refs: list[MediaRef] = []
    for attachment in attachments:
        content_type = declared_type(attachment.content_type)
        kind = _media_kind(content_type, voice)
        if kind is None or not from_discord_cdn(attachment.url):
            continue
        refs.append(
            MediaRef(
                attachment_id=attachment.id,
                kind=kind,
                content_type=content_type,
                byte_size=attachment.size,
                url=attachment.url,
                filename=attachment.filename,
                duration_seconds=attachment.duration,
            )
        )
    return tuple(refs)


def to_message(raw: RawMessage) -> Message | None:
    """Convert a platform message, or None when it must not be indexed."""
    channel, thread_id = channel_of(raw)
    if channel is None or withholds_personal_fact(raw):
        return None
    reference = raw.reference
    return Message(
        platform_message_id=raw.id,
        channel=channel,
        author=PersonRef(PLATFORM, raw.author.id),
        author_display=_display_name(raw.author),
        content=raw.content,
        created_at=raw.created_at,
        edited_at=raw.edited_at,
        reply_to_id=reference.message_id if reference is not None else None,
        thread_id=thread_id,
        # Captured as structure rather than left in the text: "what did people
        # ask me?" must not depend on scanning message bodies for a mention.
        mentions=frozenset(PersonRef(PLATFORM, u.id) for u in raw.mentions),
        media=media_of(raw),
    )


def is_ingestable(raw: RawMessage) -> bool:
    """Bots are never ingested, ourselves least of all.

    Our own answers quote the corpus; ingesting them feeds retrieval its own
    output, which compounds every time someone asks a similar question.
    """
    return (
        not raw.author.bot
        and channel_of(raw)[0] is not None
        and not withholds_personal_fact(raw)
    )


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
            # the end of the channel's history. Returning an empty page would
            # be indistinguishable from exhaustion and would mark the channel
            # fully imported, so the caller is told it could not be asked.
            log.warning("backfill.channel_unavailable", channel=str(channel))
            raise SourceUnavailable(str(channel))

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
            converted = (to_message(raw) for raw in page if is_ingestable(raw))
            return [m for m in converted if m is not None]

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
