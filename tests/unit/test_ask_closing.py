"""Closing an obligation: the two routes out of the list, and their wiring.

An ask that can be extracted and never closed makes the feature worse than
useless -- a list that only grows is one people stop reading -- so this file
is as much about *reachability* as about behaviour. `AskStateService.
record_reaction` and `CorrectionService` were both implemented, both tested,
and both had no caller at all; the tests at the bottom are the ones that would
have said so.

Two rules run through everything here:

*Identity comes from the platform event.* A reaction carries the account that
added it and an interaction carries the account that ran the command. Neither
is ever read out of message text, because text is content and content is data.

*A refusal never says which kind of refusal it is.* "That is not yours" and
"there is no such ask" are different facts, and answering one of them for any
key somebody cares to try reports whether an ask exists in a channel they may
not read.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import chatmemory
from chatmemory.adapters.discord.acl import DiscordAclResolver, DiscordAudienceResolver
from chatmemory.adapters.discord.bot import (
    CORRECTION_APPLIED,
    CORRECTION_REFUSED,
    CORRECTION_RESOLUTIONS,
    CyberFriendClient,
    _ask_label,
    _correction_note,
)
from chatmemory.adapters.discord.gateway import GatewayEventHandler, IngestClient
from chatmemory.app.ask import AskService, CorrectionRequest
from chatmemory.app.asks.corrections import CorrectionService
from chatmemory.app.asks.model import (
    AskKind,
    AskPolicy,
    AskStatus,
    ClosedBy,
    CorrectionOutcome,
    CorrectionResolution,
    ObligationRequest,
    to_group,
    to_person,
)
from chatmemory.app.asks.state import (
    ACKNOWLEDGING_REACTIONS,
    AskStateService,
    canonical_reaction,
    is_acknowledging,
)
from chatmemory.app.conversation import ConversationStore
from chatmemory.app.limits import RateLimiter
from chatmemory.domain.identity import ChannelRef, PersonRef
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    CARA,
    OPEN_CHANNEL,
    PRIVATE_CHANNEL,
    T0,
    FakeAskStore,
    ask,
    message,
    viewer,
)

SRC = Path(chatmemory.__file__).parent

LATER = T0 + timedelta(minutes=5)
NOW = T0 + timedelta(hours=1)

GENERAL, LEADERSHIP = OPEN_CHANNEL.platform_channel_id, PRIVATE_CHANNEL.platform_channel_id


def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text())
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    ]
    assert found, f"{path.name} has no function {name}"
    return found[0]


def _calls(scope: ast.AST, func: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == func
        for node in ast.walk(scope)
    )


# --- route one: an acknowledging reaction ------------------------------


@dataclass
class Emoji:
    """What discord.py reports on a raw reaction event."""

    name: str | None
    id: int | None = None


@dataclass
class ReactionPayload:
    message_id: int
    user_id: int
    emoji: Emoji


@dataclass
class Sink:
    def is_indexed(self, channel: ChannelRef) -> bool:
        return True

    async def handle_edit(self, message: object) -> None: ...

    async def handle_delete(self, platform_message_id: int, at: object = None) -> None: ...


@dataclass
class Feed:
    published: list[object] = field(default_factory=list)

    def publish(self, message: object) -> None:
        self.published.append(message)


class Reactions:
    """A seeded store, its state service, and a gateway bound to both."""

    def __init__(self, **ask_kwargs: Any) -> None:
        self.store = FakeAskStore()
        self.ask = ask(source_message_id=10, requester=ALICE, **ask_kwargs)
        self.store.asks[self.ask.key] = self.ask
        self.store.messages[10] = message(10, ALICE, "can you review the migration?")
        self.state = AskStateService(self.store, AskPolicy(stale_after=timedelta(days=21)))
        self.handler = GatewayEventHandler(Sink(), Feed(), now=lambda: LATER)
        self.handler.acknowledge_asks_with(self.state)

    async def react(
        self, person: PersonRef, emoji: str, custom_id: int | None = None
    ) -> None:
        await self.handler.on_reaction_add(
            ReactionPayload(10, person.platform_user_id, Emoji(emoji, custom_id))
        )

    async def refresh(self) -> AskStatus:
        await self.state.refresh(NOW)
        return self.store.asks[self.ask.key].status


async def test_the_addressees_tick_closes_the_ask() -> None:
    """The motivating case: today this reaction reaches nothing at all."""
    reactions = Reactions()
    await reactions.react(BOB, "✅")

    assert await reactions.refresh() is AskStatus.ANSWERED
    closed = reactions.store.asks[reactions.ask.key]
    assert closed.closed_by is ClosedBy.REACTION
    assert closed.closed_at == LATER


async def test_somebody_elses_tick_is_their_opinion_about_somebody_elses_work() -> None:
    reactions = Reactions()
    await reactions.react(CARA, "✅")
    assert await reactions.refresh() is AskStatus.OPEN


async def test_the_requesters_own_tick_does_not_close_what_they_asked_for() -> None:
    reactions = Reactions()
    await reactions.react(ALICE, "✅")
    assert await reactions.refresh() is AskStatus.OPEN


async def test_a_reaction_that_acknowledges_nothing_is_never_stored() -> None:
    """Eyes means somebody is looking, which is the opposite of finished --
    and this must not become a general record of who reacted to what."""
    reactions = Reactions()
    await reactions.react(BOB, "👀")
    await reactions.react(BOB, "🎉")

    assert reactions.store.reactions == []
    assert await reactions.refresh() is AskStatus.OPEN


async def test_a_custom_emoji_cannot_borrow_the_meaning_of_a_real_one() -> None:
    """Any guild may upload any image under the name `white_check_mark`."""
    reactions = Reactions()
    await reactions.react(BOB, "white_check_mark", custom_id=4242)

    assert reactions.store.reactions == []
    assert await reactions.refresh() is AskStatus.OPEN


async def test_the_same_reaction_twice_is_one_close() -> None:
    reactions = Reactions()
    await reactions.react(BOB, "✅")
    first = await reactions.refresh()
    closed_at = reactions.store.asks[reactions.ask.key].closed_at

    await reactions.react(BOB, "✅")
    assert await reactions.refresh() is first is AskStatus.ANSWERED
    assert reactions.store.asks[reactions.ask.key].closed_at == closed_at


async def test_a_reaction_on_a_group_ask_closes_nothing() -> None:
    """One member clearing a group obligation hides it from the rest."""
    reactions = Reactions(addressee=to_group("the platform team"))
    await reactions.react(BOB, "✅")
    assert await reactions.refresh() is AskStatus.OPEN


async def test_a_handler_nobody_attached_drops_the_event_without_raising() -> None:
    """Ingestion must survive a deployment that wired this to nothing; the
    wiring tests below are what stop that deployment being ours."""
    handler = GatewayEventHandler(Sink(), Feed(), now=lambda: LATER)
    await handler.on_reaction_add(ReactionPayload(10, BOB.platform_user_id, Emoji("✅")))


# --- the emoji itself ---------------------------------------------------


@pytest.mark.parametrize("spelling", ["☑️", "☑", " ✅ ", "✔️", "✔"])
def test_a_reaction_is_recognised_whichever_way_it_is_spelled(spelling: str) -> None:
    """U+FE0F only asks for the emoji rendering of the same character, and
    which spelling arrives depends on the client that sent it."""
    assert is_acknowledging(spelling)


async def test_a_reaction_is_stored_as_the_form_the_closing_statement_matches() -> None:
    """`CLOSE_ANSWERED_BY_REACTION` compares the stored emoji against exactly
    `ACKNOWLEDGING_REACTIONS`. A row kept in the other spelling would sit there
    for ever and close nothing."""
    store = FakeAskStore()
    assert await AskStateService(store).record_reaction(10, BOB, "☑", LATER) is True

    _, stored = store.reactions[0]
    assert stored.emoji in ACKNOWLEDGING_REACTIONS
    assert canonical_reaction("☑") == "☑️"


def test_the_acknowledging_set_is_the_one_the_spec_names() -> None:
    """Read, not invented. Eyes is the one people reach for first."""
    assert not is_acknowledging("👀")
    assert canonical_reaction("🎉") is None


# --- route two: the addressee's own word -------------------------------


def guild() -> FakeGuild:
    return FakeGuild(
        members=[
            FakeMember(ALICE.platform_user_id, frozenset({"lead"})),
            FakeMember(BOB.platform_user_id, frozenset()),
            FakeMember(CARA.platform_user_id, frozenset()),
        ],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(LEADERSHIP, allowed_roles=frozenset({"lead"})),
        ],
    )


class NeverAnswers:
    async def answer(self, question: object) -> object:  # pragma: no cover - unused
        raise AssertionError("a correction must not reach the answer path")


def service(store: FakeAskStore, *, wired: bool = True) -> AskService:
    indexed = (GENERAL, LEADERSHIP)
    asks = AskService(
        acl=DiscordAclResolver(guild(), indexed),
        audiences=DiscordAudienceResolver(guild(), indexed),
        answers=NeverAnswers(),  # type: ignore[arg-type]
        limiter=RateLimiter(),
        conversations=ConversationStore(),
    )
    if wired:
        asks.attach_corrections(CorrectionService(store, AskPolicy()))
    return asks


def seeded(**kwargs: Any) -> tuple[FakeAskStore, str]:
    store = FakeAskStore()
    item = ask(source_message_id=10, requester=ALICE, **kwargs)
    store.asks[item.key] = item
    store.messages[10] = message(10, ALICE, "can you review the migration?")
    return store, item.key


@dataclass
class Response:
    deferred: list[bool] = field(default_factory=list)

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred.append(ephemeral)


@dataclass
class Followup:
    sent: list[tuple[str, bool]] = field(default_factory=list)

    async def send(self, content: str, *, ephemeral: bool = False) -> None:
        self.sent.append((content, ephemeral))


@dataclass
class User:
    id: int


class Interaction:
    """Only what the command touches. `user` is the authenticated account."""

    def __init__(self, person: PersonRef) -> None:
        self.user = User(person.platform_user_id)
        self.response = Response()
        self.followup = Followup()


def resolve_command(asks: AskService) -> Any:
    """The real registered command object, not a re-implementation of it."""
    return CyberFriendClient(asks, guild_id=1)._build_resolve_command()


async def run_resolve(asks: AskService, actor: PersonRef, key: str, said: str) -> str:
    from discord import app_commands

    command = resolve_command(asks)
    interaction = Interaction(actor)
    await command.callback(
        interaction, ask=key, outcome=app_commands.Choice(name=said, value=said)
    )
    [(note, ephemeral)] = interaction.followup.sent
    # Always private, in both directions: the menu is a list of things
    # somebody was asked to do, and a public refusal announces that they tried.
    assert ephemeral and interaction.response.deferred == [True]
    return str(note)


async def run_autocomplete(asks: AskService, actor: PersonRef, current: str = "") -> Any:
    command = resolve_command(asks)
    which = command._params["ask"].autocomplete
    assert which is not None, "the ask option offers no list to pick from"
    return await which(Interaction(actor), current)


async def test_the_addressee_can_close_their_own_ask_from_discord() -> None:
    store, key = seeded()
    asks = service(store)

    assert await run_resolve(asks, BOB, key, "done") == CORRECTION_APPLIED["done"]
    assert await store.count_outstanding(viewer(BOB, OPEN_CHANNEL), ObligationRequest()) == 0
    assert store.corrections[key].resolution is CorrectionResolution.DONE


@pytest.mark.parametrize("said", ["not_mine", "not_a_request"])
async def test_the_addressee_can_disown_an_ask(said: str) -> None:
    """"Not mine" and "this was never a request" are both the system having
    been wrong about them, and both stop it being their obligation."""
    store, key = seeded()

    assert await run_resolve(service(store), BOB, key, said) == CORRECTION_APPLIED[said]
    assert store.corrections[key].resolution is CorrectionResolution.NOT_APPLICABLE


async def test_a_correction_outranks_a_later_extraction_pass() -> None:
    """Without this the ask reappears the next time its window is reprocessed,
    which reads as the system ignoring the person who dismissed it."""
    store, key = seeded()
    await run_resolve(service(store), BOB, key, "not_mine")

    # Exactly what the extraction worker does on a second pass over the window.
    await store.record_asks(10, [ask(source_message_id=10, requester=ALICE)])

    assert key in store.corrections
    assert await store.obligations(viewer(BOB, OPEN_CHANNEL), ObligationRequest()) == []


async def test_only_the_addressee_may_close_an_ask() -> None:
    store, key = seeded()

    assert await run_resolve(service(store), CARA, key, "done") == CORRECTION_REFUSED
    assert store.corrections == {}
    assert await store.count_outstanding(viewer(BOB, OPEN_CHANNEL), ObligationRequest()) == 1


async def test_the_requester_cannot_close_what_they_asked_for() -> None:
    store, key = seeded()
    assert await run_resolve(service(store), ALICE, key, "done") == CORRECTION_REFUSED


async def test_a_refusal_never_says_which_kind_of_refusal_it_is() -> None:
    """The motivating disclosure. Cara may not read #leadership; whether an ask
    exists there must not be answerable by trying to correct it.

    Alice can read it and is not the addressee, so hers is the "not yours"
    refusal. The two must be the same sentence.
    """
    store, key = seeded(channel=PRIVATE_CHANNEL)
    asks = service(store)

    unreadable = await run_resolve(asks, CARA, key, "done")
    not_theirs = await run_resolve(asks, ALICE, key, "done")
    no_such_ask = await run_resolve(asks, BOB, "999:request:person:discord:2", "done")

    assert unreadable == not_theirs == no_such_ask == CORRECTION_REFUSED


async def test_identity_comes_from_the_interaction_not_from_the_argument() -> None:
    """The `ask` argument is a string the person supplied, and a string is
    content. Handing Bob's key to Cara's interaction must change nothing."""
    store, key = seeded()
    assert await run_resolve(service(store), CARA, key, "done") == CORRECTION_REFUSED
    assert store.corrections == {}


async def test_an_unwired_deployment_refuses_and_discloses_nothing() -> None:
    store, key = seeded()
    assert await run_resolve(service(store, wired=False), BOB, key, "done") == CORRECTION_REFUSED
    assert store.corrections == {}


# --- the list you pick from --------------------------------------------


async def test_the_menu_offers_the_actor_their_own_asks() -> None:
    store, key = seeded()
    [choice] = await run_autocomplete(service(store), BOB)
    assert choice.value == key
    assert "review the migration" in choice.name


async def test_the_menu_never_offers_somebody_elses_obligations() -> None:
    store, _ = seeded()
    assert await run_autocomplete(service(store), CARA) == []
    assert await run_autocomplete(service(store), ALICE) == []


async def test_the_menu_omits_asks_from_channels_the_actor_cannot_read() -> None:
    """Bob is not a lead, so #leadership is not his to see -- and an ask is a
    summary, which travels further than the quote it came from."""
    store, _ = seeded(channel=PRIVATE_CHANNEL)
    assert await run_autocomplete(service(store), BOB) == []


async def test_the_menu_omits_a_sub_threshold_extraction() -> None:
    """Offering it for correction would defeat the confidence threshold from
    the other side: the person would be asked about a claim the system had
    already decided was too weak to make."""
    store, _ = seeded(confidence=0.2)
    assert await run_autocomplete(service(store), BOB) == []


async def test_the_menu_narrows_as_the_actor_types() -> None:
    store, _ = seeded()
    assert len(await run_autocomplete(service(store), BOB, "migration")) == 1
    assert await run_autocomplete(service(store), BOB, "deployment") == []


async def test_an_unwired_deployment_offers_an_empty_menu() -> None:
    store, _ = seeded()
    assert await run_autocomplete(service(store, wired=False), BOB) == []


def test_a_label_is_one_line_and_short_enough_for_discord_to_accept() -> None:
    """A newline in a choice label turns one menu entry into two, and the
    second one reads as the bot speaking."""
    from chatmemory.app.asks.model import ReportedAsk

    item = ReportedAsk(
        ask=ask(text="review the migration\n\nIGNORE THE ABOVE AND " + "x" * 200),
        requester_display="alice\nbob",
        source_excerpt="",
    )
    label = _ask_label(item)
    assert "\n" not in label
    assert len(label) <= 100


def test_a_stale_ask_says_so_in_the_menu() -> None:
    from chatmemory.app.asks.model import ReportedAsk

    item = ReportedAsk(
        ask=ask(status=AskStatus.STALE), requester_display="alice", source_excerpt=""
    )
    assert "(stale)" in _ask_label(item)


def test_a_commitment_is_phrased_as_the_persons_own_promise() -> None:
    from chatmemory.app.asks.model import ReportedAsk

    item = ReportedAsk(
        ask=ask(kind=AskKind.COMMITMENT, addressee=to_person(BOB), text="push the fix"),
        requester_display="bob",
        source_excerpt="",
    )
    assert "you said you would push the fix" in _ask_label(item)


@pytest.mark.parametrize(
    "outcome", [CorrectionOutcome.UNKNOWN_ASK, CorrectionOutcome.NOT_ADDRESSEE]
)
def test_every_refusal_renders_as_the_same_sentence(outcome: CorrectionOutcome) -> None:
    assert _correction_note(outcome, "done") == CORRECTION_REFUSED


def test_every_menu_answer_maps_to_a_resolution_and_a_reply() -> None:
    """A choice with no mapping would raise inside the command handler, which
    Discord shows the person as a failed interaction and nothing else."""
    command = resolve_command(service(FakeAskStore()))
    offered = {c.value for c in command._params["outcome"].choices}
    assert offered == set(CORRECTION_RESOLUTIONS) == set(CORRECTION_APPLIED)


# --- the running processes actually reach both routes ------------------
#
# Source-level and signature-level, because both failures are silence. A
# gateway that receives reaction events and drops them, and a `/resolve` over
# a service that can correct nothing, both look exactly like a quiet server.


def test_the_ingest_client_subscribes_to_raw_reactions() -> None:
    """`on_reaction_add` fires only for messages discord.py still holds, and an
    ask worth closing is usually old enough not to be one of them."""
    assert hasattr(IngestClient, "on_raw_reaction_add")
    assert not hasattr(IngestClient, "on_reaction_add")
    dispatch = _function(SRC / "adapters" / "discord" / "gateway.py", "on_raw_reaction_add")
    assert _calls(dispatch, "on_reaction_add")


def test_the_ingest_client_asks_for_the_reaction_intent() -> None:
    """Without it the gateway sends no reaction events at all, and the only
    symptom is obligations that never stop being reported."""
    import discord

    from chatmemory.adapters.discord.gateway import GatewayEventHandler as _H

    client = IngestClient(_H(Sink(), Feed()), guild_id=1)
    assert client.intents.reactions
    assert discord.Intents.default().reactions, "default() no longer carries it"


def test_the_ingest_process_gives_the_gateway_somewhere_to_record_reactions() -> None:
    """The whole of route one. Without this call `record_reaction` has no
    caller in the running process and the state pass finds nothing to close."""
    main = _function(SRC / "entrypoints" / "ingest.py", "main")
    assert _calls(main, "acknowledge_asks_with"), (
        "the ingest process receives every acknowledging reaction and drops it"
    )


def test_the_bot_process_gives_the_ask_service_somewhere_to_write_a_correction() -> None:
    """The whole of route two. `/resolve` is registered either way, so without
    this every attempt to close an ask is refused."""
    source = (SRC / "entrypoints" / "bot.py").read_text()
    for wiring in ("CorrectionService(", "attach_corrections("):
        assert wiring in source, f"the bot process never {wiring}"
    assert _calls(_function(SRC / "entrypoints" / "bot.py", "main"), "CorrectionService")


def test_the_bot_registers_the_resolve_command() -> None:
    """A command that is never added to the tree is never synced, and a slash
    command that was never synced does not exist to anybody."""
    setup = _function(SRC / "adapters" / "discord" / "bot.py", "setup_hook")
    assert _calls(setup, "_build_resolve_command")
    assert _calls(setup, "sync")


def test_a_correction_cannot_be_made_without_an_actor() -> None:
    """Same rule as every read path: the person is required, never defaulted."""
    for field_name in ("actor", "ask_key", "resolution"):
        assert field_name in CorrectionRequest.__annotations__
    signature = inspect.signature(AskService.correctable)
    assert signature.parameters["person"].default is inspect.Parameter.empty


def test_correcting_never_takes_the_corrector_from_the_request() -> None:
    """`CorrectionRequest` names the actor; the `Correction` written to the
    store is built from the resolved viewer, so a hand-assembled one is caught
    by the store's own predicate rather than trusted."""
    source = (SRC / "app" / "asks" / "corrections.py").read_text()
    assert "by=viewer.person" in source
