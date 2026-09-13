"""A revocation lands on the next query, in both processes that answer one.

Permissions are resolved at query time so that taking a role away takes effect
without a reindex. Every cache in front of that resolution is a way to lose
that property, and losing it is silent: the answer still looks like an answer.

So these tests do not check that a cache eventually expires. They revoke
access, dispatch the Discord event that reports it, and query again with the
clock untouched. The `assert` before each event is the one that matters just
as much -- it shows the cache really was serving the stale grant, so the
passing assertion after it is about invalidation and not about the cache
having quietly disabled itself.

Both processes are built through the functions their `main` calls, over a fake
guild swapped in behind `get_guild`, so what is under test is the production
wiring rather than a re-creation of it.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import chatmemory
from chatmemory.adapters.discord.acl import (
    DiscordAclResolver,
    DiscordAudienceResolver,
    LiveGuild,
    PermissionCaches,
)
from chatmemory.adapters.discord.gateway import (
    CachingAclResolver,
    GatewayEventHandler,
    IngestClient,
    attach_permission_listeners,
)
from chatmemory.app.ask import AskRequest, AskService
from chatmemory.config import Settings
from chatmemory.domain.audience import Audience
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.entrypoints.bot import build_bot
from chatmemory.entrypoints.mcp_server import AclClient, build_acl
from chatmemory.ports.answers import Answer, Question
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

SRC = Path(chatmemory.__file__).parent

GUILD = 7
GENERAL, INFRA = 100, 200
INFRA_ROLE = frozenset({"infra"})

BASE = {
    "discord_token": "zzz-discord-bot-token-zzz",
    "discord_guild_id": GUILD,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
    "indexed_channel_ids": f"{GENERAL} {INFRA}",
}


def settings() -> Settings:
    return Settings(**BASE)  # type: ignore[arg-type]


def ch(channel_id: int) -> ChannelRef:
    return ChannelRef("discord", channel_id)


def person(user_id: int) -> PersonRef:
    return PersonRef("discord", user_id)


def channels() -> list[FakeChannel]:
    return [
        FakeChannel(GENERAL, public=True),
        FakeChannel(INFRA, allowed_roles=INFRA_ROLE),
    ]


class RecordingAnswers:
    """Captures the audience each question was bounded by."""

    def __init__(self) -> None:
        self.audiences: list[Audience] = []

    async def answer(self, question: Question) -> Answer:
        self.audiences.append(question.audience)
        return Answer(text="ok")


class RecordingCache:
    """A cache that only records which invalidations reached it."""

    def __init__(self) -> None:
        self.dropped: list[PersonRef | None] = []

    def invalidate(self, person: PersonRef | None = None) -> None:
        self.dropped.append(person)


# --- the MCP path -------------------------------------------------------


async def test_mcp_revocation_lands_on_the_next_query() -> None:
    """The defect: a role revoked through the MCP endpoint kept reading.

    Its gateway connection registers no message handlers, which was read as
    "no handlers", so the 30-second viewer cache in front of it was never
    invalidated by anything.
    """
    guild = FakeGuild(members=[FakeMember(1, INFRA_ROLE)], text_channels=channels())
    graph = build_acl(settings())
    graph.client.get_guild = lambda _id: guild  # type: ignore[assignment,method-assign,return-value]

    assert graph.caches.live, "the ACL connection must be an invalidation source"
    before = await graph.resolver.resolve_viewer(person(1))
    assert ch(INFRA) in before.visible_channels

    guild.members = [FakeMember(1, frozenset())]
    stale = await graph.resolver.resolve_viewer(person(1))
    assert ch(INFRA) in stale.visible_channels, "expected a cache to be under test"

    # Discord reports the revocation. No clock is advanced anywhere.
    await graph.client.on_member_update(FakeMember(1), FakeMember(1))  # type: ignore[attr-defined]

    after = await graph.resolver.resolve_viewer(person(1))
    assert after.visible_channels == {ch(GENERAL)}


async def test_mcp_overwrite_change_lands_on_the_next_query() -> None:
    """A channel edit moves an unknown set of people, so everything goes."""
    guild = FakeGuild(members=[FakeMember(1, INFRA_ROLE)], text_channels=channels())
    graph = build_acl(settings())
    graph.client.get_guild = lambda _id: guild  # type: ignore[assignment,method-assign,return-value]

    assert ch(INFRA) in (await graph.resolver.resolve_viewer(person(1))).visible_channels

    guild.text_channels = [
        FakeChannel(GENERAL, public=True),
        FakeChannel(INFRA, allowed_roles=INFRA_ROLE, member_deny=frozenset({1})),
    ]
    await graph.client.on_guild_channel_update(None, None)  # type: ignore[attr-defined]

    assert ch(INFRA) not in (await graph.resolver.resolve_viewer(person(1))).visible_channels


# --- the bot path -------------------------------------------------------


async def ask_in_general(asks: AskService, answers: RecordingAnswers) -> Audience:
    """Ask publicly in #general and return the audience the answer was bounded by."""
    await asks.ask(
        AskRequest(
            asker=person(1), text="what happened?", destination=ch(GENERAL), location_id=GENERAL
        )
    )
    return answers.audiences[-1]


async def test_bot_added_member_narrows_the_audience_on_the_next_query() -> None:
    """The other half of the defect: the audience cache had no callers at all.

    A public answer may only cite channels *every* reader of the destination
    can read, so someone joining who cannot read #infra has to drop #infra out
    of #general's audience -- immediately, not a minute later.
    """
    guild = FakeGuild(
        members=[FakeMember(1, INFRA_ROLE), FakeMember(2, INFRA_ROLE)],
        text_channels=channels(),
    )
    answers = RecordingAnswers()
    graph = build_bot(settings(), answers)
    graph.client.get_guild = lambda _id: guild  # type: ignore[assignment,method-assign,return-value]

    assert graph.caches.live, "the bot client must be an invalidation source"
    first = await ask_in_general(graph.asks, answers)
    assert ch(INFRA) in first.readable_channels

    guild.members = [*guild.members, FakeMember(3, frozenset())]
    stale = await ask_in_general(graph.asks, answers)
    assert ch(INFRA) in stale.readable_channels, "expected a cache to be under test"

    await graph.client.on_member_join(FakeMember(3))  # type: ignore[attr-defined]

    after = await ask_in_general(graph.asks, answers)
    assert ch(INFRA) not in after.readable_channels
    assert after.readable_channels == {ch(GENERAL)}


async def test_bot_role_change_lands_on_the_next_query() -> None:
    guild = FakeGuild(
        members=[FakeMember(1, INFRA_ROLE), FakeMember(2, INFRA_ROLE)],
        text_channels=channels(),
    )
    answers = RecordingAnswers()
    graph = build_bot(settings(), answers)
    graph.client.get_guild = lambda _id: guild  # type: ignore[assignment,method-assign,return-value]

    assert ch(INFRA) in (await ask_in_general(graph.asks, answers)).readable_channels

    guild.members = [FakeMember(1, INFRA_ROLE), FakeMember(2, frozenset())]
    await graph.client.on_member_update(FakeMember(2), FakeMember(2))  # type: ignore[attr-defined]

    after = await ask_in_general(graph.asks, answers)
    assert after.readable_channels == {ch(GENERAL)}


# --- a cache cannot outlive its invalidation source ---------------------


async def test_without_an_invalidation_source_the_audience_cache_is_off() -> None:
    """The composition root may be handed a bare provider; that must be safe."""
    guild = FakeGuild(members=[FakeMember(1, INFRA_ROLE)], text_channels=channels())
    resolver = DiscordAudienceResolver(lambda: guild, (GENERAL, INFRA))

    assert not resolver.caching
    assert ch(INFRA) in (await resolver.resolve_for_channel(ch(GENERAL))).readable_channels

    guild.members = [*guild.members, FakeMember(3, frozenset())]

    assert ch(INFRA) not in (await resolver.resolve_for_channel(ch(GENERAL))).readable_channels
    assert resolver.size == 0


async def test_a_group_nobody_attached_disables_the_caches_it_holds() -> None:
    """Forgetting to wire the events costs latency, not correctness.

    This is the shape of the original bug -- a cache built, and nothing ever
    invalidating it -- and it now degrades to a pass-through instead of to a
    stale grant.
    """
    guild = FakeGuild(members=[FakeMember(1, INFRA_ROLE)], text_channels=channels())
    caches = PermissionCaches()
    resolver = DiscordAudienceResolver(LiveGuild(lambda: guild, caches), (GENERAL, INFRA))

    assert not caches.live
    assert not resolver.caching
    assert ch(INFRA) in (await resolver.resolve_for_channel(ch(GENERAL))).readable_channels

    guild.members = [*guild.members, FakeMember(3, frozenset())]
    assert ch(INFRA) not in (await resolver.resolve_for_channel(ch(GENERAL))).readable_channels


async def test_an_unattached_group_also_disables_the_viewer_cache() -> None:
    guild = FakeGuild(members=[FakeMember(1, INFRA_ROLE)], text_channels=channels())
    caches = PermissionCaches()
    resolver = CachingAclResolver(
        DiscordAclResolver(lambda: guild, (GENERAL, INFRA)), invalidation=caches
    )

    assert not resolver.caching
    assert ch(INFRA) in (await resolver.resolve_viewer(person(1))).visible_channels

    guild.members = [FakeMember(1, frozenset())]
    assert ch(INFRA) not in (await resolver.resolve_viewer(person(1))).visible_channels


# --- the protocol the audience resolver could not satisfy ---------------


def test_the_audience_resolver_can_be_registered_as_an_invalidator() -> None:
    """`invalidate()` took no arguments, so it never fitted `CacheInvalidator`.

    That is why it had zero production callers: it could not be handed to the
    gateway handler at all.
    """
    parameter = inspect.signature(DiscordAudienceResolver.invalidate).parameters["person"]
    assert parameter.default is None

    guild = FakeGuild(members=[FakeMember(1, INFRA_ROLE)], text_channels=channels())
    caches = PermissionCaches()
    resolver = DiscordAudienceResolver(LiveGuild(lambda: guild, caches), (GENERAL, INFRA))
    handler = GatewayEventHandler(sink=None, feed=None, caches=[resolver])  # type: ignore[arg-type]

    handler.on_member_changed(person(1))
    handler.on_permissions_changed()


# --- which events reach the caches --------------------------------------


async def test_every_permission_event_reaches_the_registered_caches() -> None:
    cache = RecordingCache()
    caches = PermissionCaches()
    caches.register(cache)
    client = AclClient(caches)

    await client.on_member_join(FakeMember(1))  # type: ignore[attr-defined]
    await client.on_member_remove(FakeMember(2))  # type: ignore[arg-type]
    await client.on_member_update(FakeMember(3), FakeMember(3))  # type: ignore[arg-type]
    await client.on_guild_role_update(None, None)  # type: ignore[arg-type]
    await client.on_guild_role_delete(None)  # type: ignore[attr-defined]
    await client.on_guild_channel_update(None, None)  # type: ignore[arg-type]

    assert cache.dropped == [person(1), person(2), person(3), None, None, None]


async def test_binding_keeps_the_handler_the_client_already_defined() -> None:
    """`IngestClient` already routes member events; replacing them loses deletes.

    Trading a stale cache for a lost gateway event is not a fix.
    """
    seen: list[PersonRef | None] = []

    class Spy:
        def on_member_changed(self, ref: PersonRef | None = None) -> None:
            seen.append(ref)

    cache = RecordingCache()
    caches = PermissionCaches()
    caches.register(cache)
    client = IngestClient(handler=Spy(), guild_id=GUILD)  # type: ignore[arg-type]
    attach_permission_listeners(client, caches)

    await client.on_member_update(FakeMember(4), FakeMember(4))  # type: ignore[arg-type]

    assert seen == [person(4)]
    assert cache.dropped == [person(4)]


# --- structural: production may not build an unwired cache --------------


def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def test_no_module_builds_a_viewer_cache_without_an_invalidation_source() -> None:
    """The constructor still allows it, for tests. Shipped code may not.

    Stated here rather than enforced in `__init__` because tightening the
    constructor would rewrite `tests/unit/test_gateway_acl.py`, which belongs
    to the TTL behaviour it documents.
    """
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        for call in _calls(path, "CachingAclResolver")
        if not any(kw.arg == "invalidation" for kw in call.keywords)
    ]
    assert not offenders, f"permission cache built with nothing to invalidate it: {offenders}"


def test_both_answering_entrypoints_bind_their_caches_to_their_client() -> None:
    """A process that answers questions must be an invalidation source."""
    for entrypoint, expected in (
        ("bot.py", ("LiveGuild(", "attach_permission_listeners(")),
        ("mcp_server.py", ("PermissionCaches()", "AclClient(caches)")),
    ):
        source = (SRC / "entrypoints" / entrypoint).read_text()
        for fragment in expected:
            assert fragment in source, f"{entrypoint} does not wire invalidation: {fragment}"
