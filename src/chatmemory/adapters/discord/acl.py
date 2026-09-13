"""Discord permission resolution.

Discord permissions are computed, never stored: an @everyone base, role
grants OR'd together, then per-channel overwrites applied in a documented
order. We do not reimplement that -- discord.py already does it correctly as
`channel.permissions_for(member)` -- we drive it and cache the results.

Reading a channel requires BOTH `view_channel` and `read_message_history`.
A member who can see a channel but not its history is the case most easily
got wrong, and it is a real one: it appears whenever a channel is opened to a
role for posting but not for backreading.

Caching those results is only sound while something drops the entries a
permission change made wrong, so the invalidation source is an object
(`PermissionCaches`) that a cache must be handed, and the only way to hand it
to a resolver the composition root builds is to pass a `LiveGuild` in place of
a bare guild provider. A resolver given a plain provider does not cache.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog

from chatmemory.domain.audience import EMPTY_AUDIENCE, Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer

log = structlog.get_logger()

PLATFORM = "discord"


class _Permissions(Protocol):
    view_channel: bool
    read_message_history: bool


class _Member(Protocol):
    id: int

    @property
    def bot(self) -> bool: ...


class _Channel(Protocol):
    id: int

    def permissions_for(self, obj: _Member, /) -> _Permissions: ...


class _Guild(Protocol):
    id: int
    members: Sequence[_Member]
    text_channels: Sequence[_Channel]

    def get_member(self, user_id: int, /) -> _Member | None: ...


GuildProvider = Callable[[], _Guild | None]
"""Returns the guild, or None while the gateway cache is still cold.

A provider rather than a guild because resolvers are constructed before the
client connects. None is treated as an empty guild everywhere, so permission
resolution fails closed rather than permissively during startup.
"""


def static_guild(guild: _Guild) -> GuildProvider:
    return lambda: guild


# --- invalidation ---------------------------------------------------------


class CacheInvalidator(Protocol):
    """A permission cache that can be dropped when an event makes it wrong."""

    def invalidate(self, person: PersonRef | None = None) -> None: ...


class PermissionCaches:
    """The invalidation source a permission cache has to be handed to work.

    This exists because the previous arrangement -- construct a cache with a
    short TTL and assume the gateway invalidates it -- was silently false in
    one of the two processes. The MCP server's gateway connection is
    deliberately handler-free, so it registered no listeners, so nothing ever
    invalidated: a revoked role kept reading a private channel for a full TTL,
    and nothing in the code said so.

    Making the source an object changes the failure. A cache with no source
    does not cache at all, and a group nobody bound to an event source
    (`attach`) reports `live is False`, which every cache checks before
    serving an entry. The cost of forgetting to wire invalidation is therefore
    latency, which is visible, instead of stale permissions, which are not.
    """

    __slots__ = ("_caches", "_source")

    def __init__(self) -> None:
        self._caches: list[CacheInvalidator] = []
        self._source: str | None = None

    @property
    def live(self) -> bool:
        """Whether permission events actually reach the registered caches."""
        return self._source is not None

    def register(self, cache: CacheInvalidator) -> None:
        self._caches.append(cache)

    def attach(self, source: str) -> None:
        """Record that `source` now dispatches permission events to this group."""
        self._source = source
        log.info("acl.invalidation_attached", source=source, caches=len(self._caches))

    def member_changed(self, person: PersonRef | None = None) -> None:
        """One person's membership or roles changed."""
        for cache in self._caches:
            cache.invalidate(person)

    def permissions_changed(self) -> None:
        """A role or a channel's overwrites changed.

        Which people that moves is unknown without redoing the work the caches
        exist to avoid, so every entry goes. Over-invalidating costs a
        recomputation; under-invalidating serves a revoked grant.
        """
        for cache in self._caches:
            cache.invalidate(None)


@dataclass(frozen=True, slots=True)
class LiveGuild:
    """A guild provider that carries the invalidation source for caches over it.

    The composition root builds the resolvers and hands them nothing but a
    `GuildProvider`, so the provider is the only channel through which a
    resolver can learn that permission changes will reach it. A resolver given
    a plain callable does not cache; one given a `LiveGuild` caches and
    registers itself with the group. There is no third form, which keeps
    "cached but never invalidated" out of the object graph rather than merely
    out of this month's review.
    """

    provider: GuildProvider
    caches: PermissionCaches

    def __call__(self) -> _Guild | None:
        return self.provider()


def invalidation_for(guild: GuildProvider | _Guild) -> PermissionCaches | None:
    """The invalidation source a resolver was given, if it was given one."""
    return guild.caches if isinstance(guild, LiveGuild) else None


def _can_read(channel: _Channel, member: _Member) -> bool:
    perms = channel.permissions_for(member)
    return bool(perms.view_channel and perms.read_message_history)


class DiscordAclResolver:
    """Resolves a single person's readable channels."""

    def __init__(self, guild: GuildProvider | _Guild, indexed: Iterable[int] = ()) -> None:
        self._guild = guild if callable(guild) else static_guild(guild)
        self._indexed = frozenset(indexed)

    def _channels(self, guild: _Guild) -> list[_Channel]:
        if not self._indexed:
            return []
        return [c for c in guild.text_channels if c.id in self._indexed]

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        guild = self._guild()
        if person.platform != PLATFORM or guild is None:
            return Viewer(person=person, visible_channels=frozenset())

        member = guild.get_member(person.platform_user_id)
        if member is None:
            # Not a member, or the member cache is cold. Either way, fail
            # closed -- a permissive default here is the exfiltration bug.
            log.info("acl.member_unresolved", user_id=person.platform_user_id)
            return Viewer(person=person, visible_channels=frozenset())

        visible = frozenset(
            ChannelRef(PLATFORM, c.id)
            for c in self._channels(guild)
            if _can_read(c, member)
        )
        return Viewer(person=person, visible_channels=visible)


class DiscordAudienceResolver:
    """Resolves what everyone receiving an answer may collectively read.

    This is a containment question, not a visibility one: a source channel is
    permitted only when *every* member who can read the destination can also
    read the source. It is computed by exact member enumeration rather than by
    comparing role sets, because per-member channel overwrites make the role
    comparison wrong in exactly the cases that matter.

    Exact enumeration requires the members intent; see `members_available`.

    It is also expensive -- every member crossed with every indexed channel --
    so the result is cached per destination, but only when the caller supplied
    an invalidation source with the guild. Without one the answer is recomputed
    every time: an audience computed before someone joined the destination
    keeps permitting sources that person cannot read, and that is not a cost
    trade anyone gets to make implicitly.
    """

    def __init__(
        self,
        guild: GuildProvider | _Guild,
        indexed: Iterable[int] = (),
        cache_ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._guild = guild if callable(guild) else static_guild(guild)
        self._indexed = frozenset(indexed)
        self._ttl = cache_ttl_seconds
        self._clock = clock
        self._cache: dict[int, tuple[float, Audience]] = {}
        self._invalidation = invalidation_for(self._guild)
        if self._invalidation is not None:
            self._invalidation.register(self)

    def _channels(self, guild: _Guild) -> list[_Channel]:
        if not self._indexed:
            return []
        return [c for c in guild.text_channels if c.id in self._indexed]

    @property
    def members_available(self) -> bool:
        """Whether the member list is usable.

        An empty member list means the members intent is missing or the cache
        is cold. Containment computed against an empty audience would be
        vacuously true -- every source would look permitted -- so callers must
        fail closed rather than proceed.
        """
        guild = self._guild()
        return guild is not None and len(guild.members) > 0

    @property
    def caching(self) -> bool:
        """Whether entries may be served: only while revocations can reach us."""
        return self._invalidation is not None and self._invalidation.live

    @property
    def size(self) -> int:
        return len(self._cache)

    def invalidate(self, person: PersonRef | None = None) -> None:
        """Drop every cached audience. `person` is accepted and ignored.

        Entries are keyed by destination channel, but one person's access
        change moves the intersection for every channel they can read, and
        working out which ones costs exactly the enumeration the cache exists
        to avoid. Clearing is cheap and cannot under-invalidate. The parameter
        stays so this satisfies `CacheInvalidator` -- without it the resolver
        could not be registered at all, which is how it ended up with a cache
        and no callers of `invalidate`.
        """
        self._cache.clear()

    async def resolve_for_channel(self, destination: ChannelRef) -> Audience:
        if destination.platform != PLATFORM:
            return EMPTY_AUDIENCE

        key = destination.platform_channel_id
        if self.caching:
            cached = self._cache.get(key)
            # The TTL is a backstop for events missed across a gateway gap,
            # not the mechanism: invalidation is.
            if cached is not None and (self._clock() - cached[0]) < self._ttl:
                return cached[1]

        audience = self._compute(destination)
        if self.caching:
            self._cache[key] = (self._clock(), audience)
        return audience

    def _compute(self, destination: ChannelRef) -> Audience:
        guild = self._guild()
        if guild is None or not self.members_available:
            # Vacuous containment would permit everything. Refuse instead.
            log.warning("audience.members_unavailable", channel=str(destination))
            return EMPTY_AUDIENCE

        dest = next(
            (c for c in guild.text_channels if c.id == destination.platform_channel_id),
            None,
        )
        if dest is None:
            return EMPTY_AUDIENCE

        humans = [m for m in guild.members if not m.bot]
        receivers = [m for m in humans if _can_read(dest, m)]
        if not receivers:
            return EMPTY_AUDIENCE

        # A source is permitted only if every receiver can read it.
        readable = frozenset(
            ChannelRef(PLATFORM, c.id)
            for c in self._channels(guild)
            if all(_can_read(c, m) for m in receivers)
        )
        return Audience(
            mode=DeliveryMode.PUBLIC_CHANNEL,
            members=frozenset(PersonRef(PLATFORM, m.id) for m in receivers),
            readable_channels=readable,
            destination=destination,
        )

    async def resolve_private(self, person: PersonRef) -> Audience:
        """An audience of one: the person's own visibility, by definition."""
        viewer = await DiscordAclResolver(self._guild, self._indexed).resolve_viewer(person)
        return Audience(
            mode=DeliveryMode.DIRECT_MESSAGE,
            members=frozenset({person}),
            readable_channels=viewer.visible_channels,
        )
