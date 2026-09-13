"""The live gateway: Discord events in, corpus mutations out.

`GatewayEventHandler` holds the event semantics and knows nothing about
discord.py, so the rules below are tested against plain objects.
`IngestClient` is the thin discord.py subclass that feeds it.

Three rules earn their place here:

- **Raw delete events are handled, not just the cached ones.** discord.py only
  dispatches `on_message_delete` for messages still in its in-memory cache,
  which after a restart holds nothing. Deletions of older messages -- exactly
  the ones long enough in the corpus to be retrievable -- arrive solely as
  `on_raw_message_delete`.
- **A delete is never gated on indexing scope.** Tombstoning a message we
  never stored is a no-op; failing to tombstone one we did store leaves
  deleted content retrievable. When the two are confused the safe direction is
  to write the tombstone.
- **The resolved-viewer cache is invalidated from here.** Permissions are
  resolved at query time precisely so a revoked role takes effect without a
  reindex; a cache that outlives the revocation would reintroduce the stale
  window the design exists to avoid.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Protocol, cast

import discord
import structlog

from chatmemory.adapters.discord.source import RawMessage, to_message
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from chatmemory.ports.acl import AclResolver

log = structlog.get_logger()

PLATFORM = "discord"


class LiveFeed(Protocol):
    """Where live messages are handed over for persistence."""

    def publish(self, message: Message) -> None: ...


class IngestSink(Protocol):
    """The corrections the gateway applies directly, bypassing the feed."""

    def is_indexed(self, channel: ChannelRef) -> bool: ...

    async def handle_edit(self, message: Message) -> None: ...

    async def handle_delete(
        self, platform_message_id: int, at: datetime | None = None
    ) -> None: ...


class RawDeletePayload(Protocol):
    message_id: int
    channel_id: int


class RawBulkDeletePayload(Protocol):
    message_ids: set[int]
    channel_id: int


class CacheInvalidator(Protocol):
    def invalidate(self, person: PersonRef | None = None) -> None: ...


# --- resolved-permission caching ----------------------------------------


class CachingAclResolver:
    """Short-TTL cache in front of an `AclResolver`.

    Resolution walks every indexed channel's overwrites for the member, which
    is cheap but not free, and a person asks several questions in a row. The
    TTL is short and the gateway invalidates on the events that can change an
    answer, so the cache narrows cost without widening the window in which a
    revoked permission still reads as granted.

    The failure direction matters: on any doubt the entry is dropped, so a
    stale *grant* cannot survive an event we saw.
    """

    def __init__(
        self,
        inner: AclResolver,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[PersonRef, tuple[float, Viewer]] = {}

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        cached = self._cache.get(person)
        if cached is not None and (self._clock() - cached[0]) < self._ttl:
            return cached[1]
        viewer = await self._inner.resolve_viewer(person)
        self._cache[person] = (self._clock(), viewer)
        return viewer

    def invalidate(self, person: PersonRef | None = None) -> None:
        """Drop one person's entry, or every entry when the change is global.

        A channel or role change alters what an unknown number of people can
        read, so it clears everything rather than guessing who was affected.
        """
        if person is None:
            self._cache.clear()
        else:
            self._cache.pop(person, None)

    @property
    def size(self) -> int:
        return len(self._cache)


# --- event handling -----------------------------------------------------


class GatewayEventHandler:
    """Translates gateway events into corpus mutations."""

    def __init__(
        self,
        sink: IngestSink,
        feed: LiveFeed,
        caches: Iterable[CacheInvalidator] = (),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sink = sink
        self._feed = feed
        self._caches = tuple(caches)
        self._now = now

    async def on_message(self, raw: RawMessage) -> None:
        if raw.author.bot:
            return
        message = to_message(raw)
        if message is None:
            return
        if not self._sink.is_indexed(message.channel):
            # Out of scope: never buffered, never stored. Indexing is opt-in
            # because the corpus is a permanent record of what people said.
            return
        self._feed.publish(message)

    async def on_message_edit(self, after: RawMessage) -> None:
        if after.author.bot:
            return
        message = to_message(after)
        if message is None:
            return
        if not self._sink.is_indexed(message.channel):
            return
        await self._sink.handle_edit(message)

    async def on_message_delete(self, raw: RawMessage) -> None:
        await self._sink.handle_delete(raw.id, self._now())

    async def on_raw_message_delete(self, payload: RawDeletePayload) -> None:
        """The only event fired for a message discord.py never cached.

        Overlaps with `on_message_delete` for cached messages; the tombstone
        is keyed by message id, so applying it twice changes nothing.
        """
        await self._sink.handle_delete(payload.message_id, self._now())

    async def on_raw_bulk_delete(self, payload: RawBulkDeletePayload) -> None:
        at = self._now()
        for message_id in payload.message_ids:
            await self._sink.handle_delete(message_id, at)
        log.info("gateway.bulk_delete", count=len(payload.message_ids))

    def on_member_changed(self, person: PersonRef | None = None) -> None:
        """Roles or membership changed: that person's resolved view is stale."""
        for cache in self._caches:
            cache.invalidate(person)

    def on_permissions_changed(self) -> None:
        """A channel's or role's permissions changed: every view may be stale."""
        for cache in self._caches:
            cache.invalidate(None)


# --- the discord.py client ----------------------------------------------


class IngestClient(discord.Client):
    """Gateway connection whose only job is feeding the handler.

    Exactly one replica may run: two containers on one bot token both identify
    and both receive every message, so the corpus silently doubles.
    """

    def __init__(
        self,
        handler: GatewayEventHandler,
        guild_id: int,
        on_connection_change: Callable[[bool], None] | None = None,
    ) -> None:
        intents = discord.Intents.default()
        # message_content: without it every message arrives with an empty
        # body. members: so a member update tells us whose view went stale.
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self._handler = handler
        self._guild_id = guild_id
        self._notify = on_connection_change

    def _connected(self, state: bool) -> None:
        if self._notify is not None:
            self._notify(state)

    async def on_ready(self) -> None:
        log.info(
            "ingest.gateway_ready",
            user=str(self.user),
            guild_id=self._guild_id,
            guilds=len(self.guilds),
        )
        self._connected(True)

    async def on_resumed(self) -> None:
        self._connected(True)

    async def on_disconnect(self) -> None:
        # Reported rather than fatal: discord.py reconnects on its own, and
        # messages missed during the gap are recovered by reconciliation.
        self._connected(False)

    async def on_message(self, message: discord.Message) -> None:
        await self._handler.on_message(cast(RawMessage, message))

    async def on_message_edit(
        self, _before: discord.Message, after: discord.Message
    ) -> None:
        await self._handler.on_message_edit(cast(RawMessage, after))

    async def on_message_delete(self, message: discord.Message) -> None:
        await self._handler.on_message_delete(cast(RawMessage, message))

    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        await self._handler.on_raw_message_delete(cast(RawDeletePayload, payload))

    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        await self._handler.on_raw_bulk_delete(cast(RawBulkDeletePayload, payload))

    async def on_member_update(self, _before: discord.Member, after: discord.Member) -> None:
        self._handler.on_member_changed(PersonRef(PLATFORM, after.id))

    async def on_member_remove(self, member: discord.Member) -> None:
        self._handler.on_member_changed(PersonRef(PLATFORM, member.id))

    async def on_guild_channel_update(
        self, _before: discord.abc.GuildChannel, _after: discord.abc.GuildChannel
    ) -> None:
        self._handler.on_permissions_changed()

    async def on_guild_role_update(self, _before: discord.Role, _after: discord.Role) -> None:
        self._handler.on_permissions_changed()
