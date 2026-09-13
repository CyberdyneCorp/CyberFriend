"""Caching resolved permissions without letting a revocation go stale.

Permissions are resolved at query time precisely so that revoking a role takes
effect on the next question with no reindexing. A cache in front of that is
only acceptable while it cannot outlive the revocation it would hide, which is
what the invalidation path and the short TTL are for.
"""

from __future__ import annotations

from chatmemory.adapters.discord.acl import DiscordAclResolver
from chatmemory.adapters.discord.gateway import CachingAclResolver, GatewayEventHandler
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

GENERAL, INFRA = 100, 200
INDEXED = (GENERAL, INFRA)


def ch(cid: int) -> ChannelRef:
    return ChannelRef("discord", cid)


def person(uid: int) -> PersonRef:
    return PersonRef("discord", uid)


class Ticker:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class CountingResolver:
    """Wraps a real resolver and counts how often it is actually consulted."""

    def __init__(self, inner: DiscordAclResolver) -> None:
        self._inner = inner
        self.calls = 0

    async def resolve_viewer(self, ref: PersonRef) -> Viewer:
        self.calls += 1
        return await self._inner.resolve_viewer(ref)


class RecordingStore:
    """Any store call at all would mean permissions cost a write somewhere."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.calls.append(name)
        raise AssertionError(f"permission resolution must not touch the store: {name}")


def guild_with(member: FakeMember) -> FakeGuild:
    return FakeGuild(
        members=[member],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(INFRA, allowed_roles=frozenset({"infra"})),
        ],
    )


def build(
    guild: FakeGuild, ttl: float = 30.0
) -> tuple[CachingAclResolver, CountingResolver, Ticker]:
    clock = Ticker()
    inner = CountingResolver(DiscordAclResolver(lambda: guild, INDEXED))
    return CachingAclResolver(inner, ttl_seconds=ttl, clock=clock), inner, clock


async def test_repeat_questions_reuse_the_resolved_set() -> None:
    cache, inner, _ = build(guild_with(FakeMember(1, frozenset({"infra"}))))

    first = await cache.resolve_viewer(person(1))
    second = await cache.resolve_viewer(person(1))

    assert first == second
    assert inner.calls == 1


async def test_the_entry_expires_with_its_ttl() -> None:
    """The TTL bounds staleness even when no event tells us anything changed."""
    cache, inner, clock = build(guild_with(FakeMember(1, frozenset({"infra"}))), ttl=30.0)

    await cache.resolve_viewer(person(1))
    clock.now = 31.0
    await cache.resolve_viewer(person(1))

    assert inner.calls == 2


async def test_each_person_is_cached_separately() -> None:
    guild = FakeGuild(
        members=[FakeMember(1, frozenset({"infra"})), FakeMember(2, frozenset())],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(INFRA, allowed_roles=frozenset({"infra"})),
        ],
    )
    cache, _, _ = build(guild)

    assert (await cache.resolve_viewer(person(1))).visible_channels == {ch(GENERAL), ch(INFRA)}
    assert (await cache.resolve_viewer(person(2))).visible_channels == {ch(GENERAL)}


async def test_revoking_a_role_removes_the_channel_from_the_next_query() -> None:
    """The invariant query-time resolution exists to provide.

    No reindexing, no corpus rewrite, no stored ACL rows to expire: the
    channel simply stops being in the viewer's set, and the predicate that
    every search binds is narrower from that moment on.
    """
    guild = guild_with(FakeMember(1, frozenset({"infra"})))
    cache, _, _ = build(guild)
    store = RecordingStore()
    handler = GatewayEventHandler(sink=store, feed=store, caches=[cache])  # type: ignore[arg-type]

    before = await cache.resolve_viewer(person(1))
    assert ch(INFRA) in before.visible_channels

    # The role is taken away, and Discord tells us so.
    guild.members = [FakeMember(1, frozenset())]
    handler.on_member_changed(person(1))

    after = await cache.resolve_viewer(person(1))
    assert ch(INFRA) not in after.visible_channels
    assert after.visible_channels == {ch(GENERAL)}
    assert store.calls == []  # nothing was reindexed, rewritten, or re-embedded


async def test_a_channel_permission_change_clears_every_viewer() -> None:
    """One channel's overwrites can change what an unknown number of people read."""
    guild = guild_with(FakeMember(1, frozenset({"infra"})))
    cache, inner, _ = build(guild)
    handler = GatewayEventHandler(sink=None, feed=None, caches=[cache])  # type: ignore[arg-type]

    await cache.resolve_viewer(person(1))
    handler.on_permissions_changed()
    await cache.resolve_viewer(person(1))

    assert inner.calls == 2
    assert cache.size == 1


async def test_a_person_who_was_never_resolved_is_not_an_error() -> None:
    cache, _, _ = build(guild_with(FakeMember(1)))
    cache.invalidate(person(404))
    assert cache.size == 0
