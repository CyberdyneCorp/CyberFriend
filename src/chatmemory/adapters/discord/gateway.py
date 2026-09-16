"""The live gateway: Discord events in, corpus mutations out.

`GatewayEventHandler` holds the event semantics and knows nothing about
discord.py, so the rules below are tested against plain objects.
`IngestClient` is the thin discord.py subclass that feeds it.

Four rules earn their place here:

- **Raw delete events are handled, not just the cached ones.** discord.py only
  dispatches `on_message_delete` for messages still in its in-memory cache,
  which after a restart holds nothing. Deletions of older messages -- exactly
  the ones long enough in the corpus to be retrievable -- arrive solely as
  `on_raw_message_delete`.
- **A delete is never gated on indexing scope.** Tombstoning a message we
  never stored is a no-op; failing to tombstone one we did store leaves
  deleted content retrievable. When the two are confused the safe direction is
  to write the tombstone.
- **An acknowledging reaction is an ingest event, not a query-time one.**
  The addressee ticking the message they were asked on is the one closing
  signal that leaves no message behind, so if nothing here records it the ask
  stays open for ever and the list only grows. Raw again, for the same reason
  deletes are: the message being acknowledged is usually old enough to have
  fallen out of discord.py's cache, which is exactly when it matters.
- **The resolved-viewer cache is invalidated from here.** Permissions are
  resolved at query time precisely so a revoked role takes effect without a
  reindex; a cache that outlives the revocation would reintroduce the stale
  window the design exists to avoid. `attach_permission_listeners` is how a
  client that is not the ingest client -- the bot's, and the MCP server's
  handler-free ACL connection -- gets the same wiring without inheriting the
  ingest client's message handlers.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Protocol, cast

import discord
import structlog

from chatmemory.adapters.discord.acl import CacheInvalidator, PermissionCaches
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


class RawEmoji(Protocol):
    """The emoji on a reaction event, as discord.py reports it."""

    name: str | None
    id: int | None


class RawReactionPayload(Protocol):
    message_id: int
    user_id: int
    emoji: RawEmoji


class AskAcknowledgements(Protocol):
    """Where a reaction that could close an ask is recorded.

    A port rather than the service itself, so the event semantics below stay
    testable without the ask package -- and so nothing in this file is tempted
    to decide *whether* the reaction closes anything. That is a property of the
    ask row, and it is settled by a SQL predicate.
    """

    async def record_reaction(
        self, source_message_id: int, person: PersonRef, emoji: str, at: datetime
    ) -> bool: ...


def reaction_emoji(emoji: RawEmoji) -> str | None:
    """The reaction as a plain character, or None when it cannot be one.

    A custom server emoji is refused outright. It arrives with an id and a name
    its uploader chose, so a guild that uploads any image at all under the name
    `white_check_mark` would otherwise be able to close other people's
    obligations with it -- the addressee check still holds, but the meaning of
    the gesture would no longer be the one the spec names.
    """
    if emoji.id is not None:
        return None
    return emoji.name


# --- resolved-permission caching ----------------------------------------


class CachingAclResolver:
    """Short-TTL cache in front of an `AclResolver`.

    Resolution walks every indexed channel's overwrites for the member, which
    is cheap but not free, and a person asks several questions in a row.

    `invalidation` is what makes the cache honest, and the TTL is only a
    backstop for events missed across a gateway gap. Given a group, entries
    are served only while that group is bound to a live event source, so a
    group that was built and never attached degrades this to a pass-through
    rather than to a stale grant. Omitting it leaves the old TTL-only
    behaviour, which is correct for a test and not correct for a process:
    production builds these through `PermissionCaches`, never directly, and
    `tests/unit/test_acl_cache_invalidation.py` asserts that.

    The failure direction matters: on any doubt the entry is dropped, so a
    stale *grant* cannot survive an event we saw.
    """

    def __init__(
        self,
        inner: AclResolver,
        *,
        invalidation: PermissionCaches | None = None,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[PersonRef, tuple[float, Viewer]] = {}
        self._invalidation = invalidation
        if invalidation is None:
            log.warning(
                "acl.cache_without_invalidation",
                hint="only a TTL bounds staleness; pass invalidation=PermissionCaches()",
            )
        else:
            invalidation.register(self)

    @property
    def caching(self) -> bool:
        """Whether entries may be served: only while revocations can reach us."""
        return self._invalidation is None or self._invalidation.live

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        if self.caching:
            cached = self._cache.get(person)
            if cached is not None and (self._clock() - cached[0]) < self._ttl:
                return cached[1]
        viewer = await self._inner.resolve_viewer(person)
        if self.caching:
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


# --- binding invalidation to a client ------------------------------------

_Listener = Callable[..., Awaitable[None]]


def _bind(client: discord.Client, name: str, listener: _Listener) -> None:
    """Register `listener` for `name`, keeping any handler the class defined.

    discord.py dispatches by looking the handler up on the instance, so this
    is the registration `@client.event` performs. Chaining rather than
    overwriting matters because `IngestClient` already defines some of these:
    replacing its `on_member_update` would trade a stale cache for a lost
    event, which is the same bug wearing a different hat.
    """
    existing = cast("_Listener | None", getattr(client, name, None))
    if existing is None:
        setattr(client, name, listener)
        return

    prior: _Listener = existing

    async def chained(*args: object, **kwargs: object) -> None:
        await prior(*args, **kwargs)
        await listener(*args, **kwargs)

    setattr(client, name, chained)


def attach_permission_listeners(client: discord.Client, caches: PermissionCaches) -> None:
    """Make `client` the event source that keeps `caches` honest.

    Every process that answers questions has to call this, because a
    permission cache only serves entries while its group is attached. The two
    callers are deliberately unlike each other -- the bot's client and the MCP
    server's handler-free ACL connection -- and sharing one wiring is what
    stops the second from quietly having none, which is exactly what happened.

    Which events, and why these. Membership and role changes move one known
    person, so they invalidate that person. Channel updates carry
    permission-overwrite edits and move an unknown set of people, so they
    invalidate everything. Role deletion counts as a permission change because
    deleting a role revokes what it granted. Creation events are absent on
    purpose: a role nobody holds and a channel nobody is in cannot widen
    anybody's view, and the first update that changes that is dispatched.
    """

    async def on_member_join(member: discord.Member) -> None:
        caches.member_changed(PersonRef(PLATFORM, member.id))

    async def on_member_remove(member: discord.Member) -> None:
        caches.member_changed(PersonRef(PLATFORM, member.id))

    async def on_member_update(_before: discord.Member, after: discord.Member) -> None:
        caches.member_changed(PersonRef(PLATFORM, after.id))

    async def on_guild_role_update(_before: discord.Role, _after: discord.Role) -> None:
        caches.permissions_changed()

    async def on_guild_role_delete(_role: discord.Role) -> None:
        caches.permissions_changed()

    async def on_guild_channel_update(
        _before: discord.abc.GuildChannel, _after: discord.abc.GuildChannel
    ) -> None:
        caches.permissions_changed()

    listeners: tuple[_Listener, ...] = (
        on_member_join,
        on_member_remove,
        on_member_update,
        on_guild_role_update,
        on_guild_role_delete,
        on_guild_channel_update,
    )
    for listener in listeners:
        _bind(client, listener.__name__, listener)

    caches.attach(type(client).__name__)


# --- event handling -----------------------------------------------------


class GatewayEventHandler:
    """Translates gateway events into corpus mutations."""

    def __init__(
        self,
        sink: IngestSink,
        feed: LiveFeed,
        caches: Iterable[CacheInvalidator] = (),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        asks: AskAcknowledgements | None = None,
    ) -> None:
        self._sink = sink
        self._feed = feed
        self._caches = tuple(caches)
        self._now = now
        self._asks = asks

    def acknowledge_asks_with(self, asks: AskAcknowledgements) -> None:
        """Route acknowledging reactions to `asks`.

        Attached after construction rather than passed in, because the ask
        pipeline is built from the database engine and this handler is built
        from the gateway, and the ingest process assembles them in that order.
        Same shape, and the same reason, as `attach_permission_listeners`.

        Not optional in practice: a process that never calls this receives
        reaction events and drops them, so an ask can be extracted and can
        never be closed by the one signal that leaves no message behind.
        """
        self._asks = asks

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

    async def on_reaction_add(self, payload: RawReactionPayload) -> None:
        """Hand an acknowledging reaction over; ignore everything else.

        Not gated on indexing scope, and not filtered by who reacted. The
        store keeps a reaction only for a message it already holds, and only
        the addressee's counts when the ask is closed -- so both questions are
        answered where the answer lives, once, in SQL.
        """
        if self._asks is None:
            return
        emoji = reaction_emoji(payload.emoji)
        if emoji is None:
            return
        await self._asks.record_reaction(
            payload.message_id,
            PersonRef(PLATFORM, payload.user_id),
            emoji,
            self._now(),
        )

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
        # reactions: stated rather than inherited from `default()`. A tick from
        # the addressee is one of the two events that close an ask, and an
        # intent silently dropped in a future refactor would show up only as
        # obligations that never stop being reported.
        intents.reactions = True
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

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        """The raw event only: a tick lands on a message, not on a cache entry.

        `on_reaction_add` fires solely for messages discord.py still holds, and
        an ask worth closing is usually hours or days old by the time somebody
        acknowledges it -- which is to say, never cached.
        """
        await self._handler.on_reaction_add(cast(RawReactionPayload, payload))

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
