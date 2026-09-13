"""Permission resolution: one person's access, and an audience's collective access.

These are tested against separate fixtures on purpose. The two questions look
alike, and answering the audience question with the viewer implementation is
the defect this suite exists to catch.
"""

from __future__ import annotations

import pytest

from chatmemory.adapters.discord.acl import DiscordAclResolver, DiscordAudienceResolver
from chatmemory.domain.identity import ChannelRef, PersonRef
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

GENERAL, INFRA, LEADERSHIP = 100, 200, 300


def ch(cid: int) -> ChannelRef:
    return ChannelRef("discord", cid)


def person(uid: int) -> PersonRef:
    return PersonRef("discord", uid)


@pytest.fixture
def guild() -> FakeGuild:
    """Everyone reads #general. Infra reads #infra. Only leads read #leadership."""
    return FakeGuild(
        members=[
            FakeMember(1, frozenset({"lead", "infra"})),  # broad access
            FakeMember(2, frozenset({"infra"})),
            FakeMember(3, frozenset()),  # general only
            FakeMember(99, frozenset({"lead"}), bot=True),  # bots are not an audience
        ],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(INFRA, allowed_roles=frozenset({"infra", "lead"})),
            FakeChannel(LEADERSHIP, allowed_roles=frozenset({"lead"})),
        ],
    )


INDEXED = (GENERAL, INFRA, LEADERSHIP)


# --- viewer visibility -------------------------------------------------


async def test_viewer_sees_only_permitted_channels(guild: FakeGuild) -> None:
    viewer = await DiscordAclResolver(guild, INDEXED).resolve_viewer(person(2))
    assert viewer.visible_channels == {ch(GENERAL), ch(INFRA)}


async def test_non_member_sees_nothing(guild: FakeGuild) -> None:
    """Fail closed: an unresolvable person gets an empty set, not a default."""
    viewer = await DiscordAclResolver(guild, INDEXED).resolve_viewer(person(404))
    assert viewer.visible_channels == frozenset()


async def test_view_without_history_is_denied() -> None:
    """The easiest case to get wrong: visible, but cannot read back."""
    g = FakeGuild(
        members=[FakeMember(1, frozenset({"poster"}))],
        text_channels=[
            FakeChannel(INFRA, allowed_roles=frozenset({"poster"}),
                        no_history_roles=frozenset({"poster"}))
        ],
    )
    viewer = await DiscordAclResolver(g, (INFRA,)).resolve_viewer(person(1))
    assert viewer.visible_channels == frozenset()


async def test_unindexed_channels_are_never_visible(guild: FakeGuild) -> None:
    viewer = await DiscordAclResolver(guild, (GENERAL,)).resolve_viewer(person(1))
    assert viewer.visible_channels == {ch(GENERAL)}


# --- audience containment ----------------------------------------------


async def test_public_channel_audience_excludes_restricted_sources(guild: FakeGuild) -> None:
    """#general is read by everyone, so only #general is safe to cite there."""
    audience = await DiscordAudienceResolver(guild, INDEXED).resolve_for_channel(ch(GENERAL))
    assert audience.readable_channels == {ch(GENERAL)}
    assert not audience.permits(ch(LEADERSHIP))


async def test_restricted_channel_audience_may_cite_what_all_of_them_read(
    guild: FakeGuild,
) -> None:
    """Everyone who reads #leadership also reads #infra and #general."""
    audience = await DiscordAudienceResolver(guild, INDEXED).resolve_for_channel(ch(LEADERSHIP))
    assert audience.readable_channels == {ch(GENERAL), ch(INFRA), ch(LEADERSHIP)}


async def test_audience_is_not_the_askers_visibility(guild: FakeGuild) -> None:
    """The distinction this whole module exists for.

    Member 1 can read everything. Asking in #general must not widen what may
    be cited there -- the audience is the room, not the asker.
    """
    viewer = await DiscordAclResolver(guild, INDEXED).resolve_viewer(person(1))
    audience = await DiscordAudienceResolver(guild, INDEXED).resolve_for_channel(ch(GENERAL))

    assert ch(LEADERSHIP) in viewer.visible_channels
    assert ch(LEADERSHIP) not in audience.readable_channels
    assert audience.readable_channels < viewer.visible_channels


async def test_member_overwrite_breaks_role_subset_reasoning() -> None:
    """A role-subset check would wrongly permit #infra here.

    Every *role* that reads #general also reads #infra, but member 7 is
    denied #infra individually while still reading #general. Containment must
    therefore fail -- and only member-level evaluation sees that.
    """
    g = FakeGuild(
        members=[
            FakeMember(6, frozenset({"staff"})),
            FakeMember(7, frozenset({"staff"})),
        ],
        text_channels=[
            FakeChannel(GENERAL, allowed_roles=frozenset({"staff"})),
            FakeChannel(INFRA, allowed_roles=frozenset({"staff"}),
                        member_deny=frozenset({7})),
        ],
    )
    audience = await DiscordAudienceResolver(g, (GENERAL, INFRA)).resolve_for_channel(ch(GENERAL))
    assert audience.permits(ch(GENERAL))
    assert not audience.permits(ch(INFRA))


async def test_member_allow_overwrite_is_honoured() -> None:
    """The mirror case: an individually-granted member still counts as a receiver."""
    g = FakeGuild(
        members=[FakeMember(8, frozenset()), FakeMember(9, frozenset())],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(INFRA, member_allow=frozenset({8})),
        ],
    )
    audience = await DiscordAudienceResolver(g, (GENERAL, INFRA)).resolve_for_channel(ch(INFRA))
    assert audience.members == {person(8)}
    assert audience.permits(ch(INFRA))


async def test_bots_are_not_part_of_an_audience(guild: FakeGuild) -> None:
    audience = await DiscordAudienceResolver(guild, INDEXED).resolve_for_channel(ch(LEADERSHIP))
    assert person(99) not in audience.members


async def test_empty_member_cache_fails_closed() -> None:
    """Containment over an empty audience is vacuously true.

    Every source would look permitted. Missing the members intent must
    therefore produce an empty audience, not a permissive one.
    """
    g = FakeGuild(members=[], text_channels=[FakeChannel(GENERAL, public=True)])
    resolver = DiscordAudienceResolver(g, (GENERAL,))
    assert not resolver.members_available
    audience = await resolver.resolve_for_channel(ch(GENERAL))
    assert audience.readable_channels == frozenset()


async def test_unknown_destination_yields_empty_audience(guild: FakeGuild) -> None:
    audience = await DiscordAudienceResolver(guild, INDEXED).resolve_for_channel(ch(999))
    assert audience.readable_channels == frozenset()


async def test_private_audience_matches_that_persons_visibility(guild: FakeGuild) -> None:
    resolver = DiscordAudienceResolver(guild, INDEXED)
    audience = await resolver.resolve_private(person(2))
    viewer = await DiscordAclResolver(guild, INDEXED).resolve_viewer(person(2))
    assert audience.readable_channels == viewer.visible_channels
    assert audience.is_private
