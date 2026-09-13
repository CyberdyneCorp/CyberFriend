"""Discord permission resolution.

Discord permissions are computed, never stored: an @everyone base, role
grants OR'd together, then per-channel overwrites applied in a documented
order. We do not reimplement that -- discord.py already does it correctly as
`channel.permissions_for(member)` -- we drive it and cache the results.

Reading a channel requires BOTH `view_channel` and `read_message_history`.
A member who can see a channel but not its history is the case most easily
got wrong, and it is a real one: it appears whenever a channel is opened to a
role for posting but not for backreading.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
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
    """

    def __init__(
        self,
        guild: GuildProvider | _Guild,
        indexed: Iterable[int] = (),
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._guild = guild if callable(guild) else static_guild(guild)
        self._indexed = frozenset(indexed)
        self._ttl = cache_ttl_seconds
        self._cache: dict[int, tuple[float, Audience]] = {}

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

    def invalidate(self) -> None:
        self._cache.clear()

    async def resolve_for_channel(self, destination: ChannelRef) -> Audience:
        if destination.platform != PLATFORM:
            return EMPTY_AUDIENCE

        cached = self._cache.get(destination.platform_channel_id)
        if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
            return cached[1]

        audience = self._compute(destination)
        self._cache[destination.platform_channel_id] = (time.monotonic(), audience)
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
