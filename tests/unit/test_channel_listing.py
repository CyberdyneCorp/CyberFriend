"""Which archived channels one person may be told about.

The feature is an intersection, and everything that matters is what it must
not disclose. A list of indexed channels is a list of channel names, and
naming a channel tells somebody it exists -- so the reply says nothing at all
about what the intersection removed: not the names, not the count, not that
there were any.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chatmemory.adapters.discord.bot import NO_CHANNELS_FOR_YOU, _channels_message
from chatmemory.app.channel_listing import ChannelListing, ChannelListingService
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"
DISCORD_BOT = SRC / "adapters" / "discord" / "bot.py"

LEO = PersonRef(platform="discord", platform_user_id=7)
PUBLIC = 100
TEAM = 200
PRIVATE = 300


class FakeScope:
    def __init__(self, indexed: set[int]) -> None:
        self._indexed = indexed

    def current(self) -> frozenset[int]:
        return frozenset(self._indexed)


class FakeAcl:
    """Resolves one person's readable channels, failing closed like the real one."""

    def __init__(self, readable: dict[int, set[int]]) -> None:
        self._readable = readable
        self.asked: list[PersonRef] = []

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        self.asked.append(person)
        channels = self._readable.get(person.platform_user_id, set())
        return Viewer(
            person=person,
            visible_channels=frozenset(ChannelRef("discord", c) for c in channels),
        )


def service(indexed: set[int], readable: dict[int, set[int]]) -> ChannelListingService:
    return ChannelListingService(FakeScope(indexed), FakeAcl(readable))  # type: ignore[arg-type]


# --- the intersection ---------------------------------------------------


async def test_an_archived_channel_the_asker_can_read_is_listed() -> None:
    listing = await service({PUBLIC}, {7: {PUBLIC}}).for_person(LEO)
    assert [c.platform_channel_id for c in listing.channels] == [PUBLIC]


async def test_an_archived_channel_the_asker_cannot_read_is_absent() -> None:
    """The one that matters. Naming it tells them a private channel exists."""
    listing = await service({PUBLIC, PRIVATE}, {7: {PUBLIC}}).for_person(LEO)
    assert [c.platform_channel_id for c in listing.channels] == [PUBLIC]


async def test_a_readable_channel_that_is_not_archived_is_absent() -> None:
    """Readable is not the same as archived; this lists what is stored."""
    listing = await service({PUBLIC}, {7: {PUBLIC, TEAM}}).for_person(LEO)
    assert [c.platform_channel_id for c in listing.channels] == [PUBLIC]


async def test_an_unresolvable_person_is_shown_nothing() -> None:
    """The resolver fails closed by contract; this must not paper over it."""
    listing = await service({PUBLIC, PRIVATE}, {}).for_person(LEO)
    assert listing.empty


async def test_nothing_archived_is_empty_without_asking_the_resolver() -> None:
    acl = FakeAcl({7: {PUBLIC}})
    listing = await ChannelListingService(FakeScope(set()), acl).for_person(LEO)  # type: ignore[arg-type]
    assert listing.empty
    assert acl.asked == [], "no permission question to ask when nothing is archived"


async def test_scope_is_read_per_request_not_captured() -> None:
    """Scope changes without a redeploy; a captured copy would name a channel
    somebody un-indexed this morning."""
    scope = FakeScope({PUBLIC, TEAM})
    listing_service = ChannelListingService(scope, FakeAcl({7: {PUBLIC, TEAM}}))  # type: ignore[arg-type]

    assert len((await listing_service.for_person(LEO)).channels) == 2
    scope._indexed = {PUBLIC}  # noqa: SLF001 - standing in for an operator's edit
    assert len((await listing_service.for_person(LEO)).channels) == 1


async def test_access_is_resolved_per_request_not_captured() -> None:
    """Somebody who lost access to a channel since it was archived must not be
    told it is there."""
    readable = {7: {PUBLIC, TEAM}}
    listing_service = ChannelListingService(FakeScope({PUBLIC, TEAM}), FakeAcl(readable))  # type: ignore[arg-type]

    assert len((await listing_service.for_person(LEO)).channels) == 2
    readable[7] = {PUBLIC}
    assert len((await listing_service.for_person(LEO)).channels) == 1


# --- what the reply may not disclose ------------------------------------


def test_the_listing_carries_no_count_of_what_was_withheld() -> None:
    """A field that exists gets rendered eventually. "and 4 you cannot read"
    tells somebody four private archived channels exist."""
    assert {f.name for f in ChannelListing.__dataclass_fields__.values()} == {"channels"}


def test_the_reply_is_identical_whether_or_not_others_exist() -> None:
    """Two different wordings would make the reply a test for whether private
    archives exist."""
    assert _channels_message(ChannelListing()) == NO_CHANNELS_FOR_YOU


def test_the_reply_lists_readable_channels_as_mentions() -> None:
    listing = ChannelListing((ChannelRef("discord", PUBLIC), ChannelRef("discord", TEAM)))
    message = _channels_message(listing)
    assert f"<#{PUBLIC}>" in message
    assert f"<#{TEAM}>" in message
    assert "(2)" in message


@pytest.mark.parametrize("channels", [(), (ChannelRef("discord", PUBLIC),)])
def test_the_reply_never_names_a_total(channels: tuple[ChannelRef, ...]) -> None:
    message = _channels_message(ChannelListing(channels))
    for leak in ("cannot read", "can't read", "hidden", "withheld", "more"):
        assert leak not in message.lower()


# --- the command --------------------------------------------------------


def _command_call(name: str) -> ast.Call:
    for node in ast.walk(ast.parse(DISCORD_BOT.read_text())):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "command":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == name
            ):
                return node
    raise AssertionError(f"no command named {name!r} is registered")


def test_the_command_is_registered() -> None:
    assert _command_call("channels") is not None


def test_the_reply_is_ephemeral() -> None:
    """Answered in a channel, a non-ephemeral reply would show everyone there
    which channels the asker can read."""
    source = DISCORD_BOT.read_text()
    start = source.index("def _build_channels_command")
    body = source[start : source.index("def _build_indexing_command", start)]
    assert body.count("ephemeral=True") >= 2, "both the defer and the reply"
    assert "ephemeral=False" not in body


# --- reached from the process that runs ---------------------------------


def _function(path: Path, name: str) -> ast.AST:
    found = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    ]
    assert found, f"{path.name} has no function {name}"
    return found[0]


def _calls(scope: ast.AST, func: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == func
    ]


def test_the_bot_entrypoint_attaches_the_listing() -> None:
    """Registered but never attached, the command answers "unavailable" for
    ever -- which is this project's usual way of shipping nothing."""
    graph = _function(SRC / "entrypoints" / "bot.py", "build_bot")
    assert _calls(graph, "attach_channel_listing"), (
        "build_bot must attach the listing, or /channels is permanently unavailable"
    )
    assert _calls(graph, "build_channel_listing_service")


def test_the_listing_reads_the_same_scope_index_writes() -> None:
    """A second scope would let `/index` change one and `/channels` read the
    other, so an archived channel would never appear."""
    source = (SRC / "entrypoints" / "bot.py").read_text()
    start = source.index("def build_channel_listing_service")
    body = source[start : source.index("def build_indexing_service", start)]
    assert "scope" in body
    assert "build_channel_listing(guild, scope)" in body


def test_it_uses_the_resolver_that_scopes_retrieval() -> None:
    """A second permission check could disagree with what the person can
    actually search, and the disagreement would be invisible."""
    source = (SRC / "composition.py").read_text()
    start = source.index("def build_channel_listing")
    body = source[start : source.index("def build_answers", start)]
    assert "DiscordAclResolver" in body
