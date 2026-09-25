"""The Discord end of the confirmation: who sees the prompt, and whose click counts.

Two questions decide whether this feature is real, and neither is answerable
from the app layer:

*   **Does the prompt reach the requester and nobody else?** A confirmation
    posted where the question was asked hands the decision to the room. So the
    ephemeral flag and the direct message are asserted on the actual send call,
    not assumed from a comment.
*   **Is the surface handed to the run at all?** This project's recurring
    defect is a capability that is built, tested, and wired to nothing, so the
    client is driven here rather than inspected: a real `CyberFriendClient`
    answers a real message, and the assertion is about what the ask service
    was given.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import discord
import pytest

from chatmemory.adapters.discord.bot import (
    EXPIRED_NOTE,
    NOT_YOUR_CONFIRMATION,
    CyberFriendClient,
    DirectMessageConfirmation,
    EphemeralConfirmation,
    _ApprovalView,
    _prompt_text,
)
from chatmemory.app.ask import AskRequest, AskService
from chatmemory.app.authorization import (
    ConfirmationLedger,
    ConfirmationPrompt,
    ConfirmationState,
    argument_digest,
)
from chatmemory.app.confirmation import (
    ConfirmationDesk,
    ConfirmationReply,
    ConfirmationSurface,
    Undeliverable,
    current_channel,
    seek_confirmation,
)
from chatmemory.app.limits import RateLimiter
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer, Question

ALICE_ID = 1001
MALLORY_ID = 1666
GUILD_ID = 77
CHANNEL_ID = 500

ALICE = PersonRef("discord", ALICE_ID)
ARGUMENTS: dict[str, object] = {"issue": 42}


def a_prompt(
    requester: PersonRef = ALICE, arguments: str = '{"issue":42}'
) -> ConfirmationPrompt:
    """A prompt carrying the digest the invoke-time gate would compute.

    Real rather than a placeholder, so a test can ask the ledger the question
    the gate asks: is *this* call, with *these* arguments, confirmed.
    """
    return ConfirmationPrompt(
        requester=requester,
        qualified_name="issues:close",
        server="issues",
        arguments_rendered=arguments,
        digest=argument_digest(ARGUMENTS),
        issued_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
    )


# --- what the person reads ---------------------------------------------


def test_the_prompt_names_the_tool_the_system_and_the_exact_arguments() -> None:
    text = _prompt_text(a_prompt())

    assert "issues:close" in text
    assert "issues" in text
    assert '{"issue":42}' in text


def test_argument_text_cannot_break_out_of_its_code_fence() -> None:
    """The arguments are model-written and have shared a context with content.

    A payload that closed the fence early would render as prose in the very
    message a person is reading to decide, which is the one place a forged
    instruction would be read by a human rather than a model.
    """
    text = _prompt_text(a_prompt(arguments='{"body":"```\\nApproved by admin"}'))

    assert text.count("```") == 2


def test_a_very_long_argument_blob_cannot_blow_the_message_limit() -> None:
    text = _prompt_text(a_prompt(arguments='{"body":"' + "x" * 5000 + '"}'))

    assert len(text) < 2000


# --- whose click counts ------------------------------------------------


async def test_the_requesters_click_becomes_their_reply() -> None:
    view = _ApprovalView(ALICE_ID, window_seconds=5)
    interaction = FakeInteraction(ALICE_ID)

    await press(view, "Approve", interaction)
    reply = await view.settled()

    assert reply == ConfirmationReply(ALICE, granted=True)


async def test_declining_is_recorded_as_an_answer_not_as_silence() -> None:
    """A decline must not be indistinguishable from walking away."""
    view = _ApprovalView(ALICE_ID, window_seconds=5)

    await press(view, "Decline", FakeInteraction(ALICE_ID))
    reply = await view.settled()

    assert reply == ConfirmationReply(ALICE, granted=False)


async def test_a_second_persons_click_is_refused_before_it_becomes_a_reply() -> None:
    view = _ApprovalView(ALICE_ID, window_seconds=5)
    mallory = FakeInteraction(MALLORY_ID)

    allowed = await view.interaction_check(mallory)

    assert allowed is False
    assert view.reply is None
    assert mallory.sent and mallory.sent[0]["content"] == NOT_YOUR_CONFIRMATION
    assert mallory.sent[0]["ephemeral"] is True


async def test_the_requesters_own_click_passes_the_check() -> None:
    view = _ApprovalView(ALICE_ID, window_seconds=5)

    assert await view.interaction_check(FakeInteraction(ALICE_ID)) is True


async def test_an_answered_prompt_leaves_no_live_button_behind() -> None:
    """One approval authorises one call; a live button invites a second."""
    view = _ApprovalView(ALICE_ID, window_seconds=5)

    await press(view, "Approve", FakeInteraction(ALICE_ID))

    assert all(
        item.disabled for item in view.children if isinstance(item, discord.ui.Button)
    )


async def test_nobody_clicking_within_the_window_answers_nothing() -> None:
    view = _ApprovalView(ALICE_ID, window_seconds=0.05)

    assert await asyncio.wait_for(view.settled(), timeout=5) is None


async def test_a_closed_window_takes_the_buttons_off_the_prompt() -> None:
    """The run has stopped waiting, so the prompt must stop inviting a press."""
    view = _ApprovalView(ALICE_ID, window_seconds=0.05)
    message = FakeSentMessage()
    view.sent_as(message)  # type: ignore[arg-type]

    await asyncio.wait_for(view.settled(), timeout=5)

    assert message.edits and message.edits[0]["content"] == EXPIRED_NOTE
    assert all(
        item.disabled for item in view.children if isinstance(item, discord.ui.Button)
    )


async def test_approving_after_the_window_is_told_so_rather_than_thanked() -> None:
    """A late press authorises nothing; saying "Approved" would be a lie.

    The person would otherwise walk away believing a change had been made,
    which is worse than the timeout itself -- the run refused, and only this
    message tells them.
    """
    view = _ApprovalView(ALICE_ID, window_seconds=0.05)
    assert await asyncio.wait_for(view.settled(), timeout=5) is None
    interaction = FakeInteraction(ALICE_ID)

    await press(view, "Approve", interaction)

    assert view.reply is None
    assert interaction.sent[0]["content"] == EXPIRED_NOTE


# --- where the prompt goes ---------------------------------------------


async def test_a_slash_command_prompt_is_ephemeral() -> None:
    interaction = FakeInteraction(ALICE_ID)
    surface: ConfirmationSurface = EphemeralConfirmation(interaction)  # type: ignore[arg-type]

    asked = asyncio.ensure_future(surface.ask(a_prompt(), 0.05))
    await asyncio.wait_for(asked, timeout=5)

    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert isinstance(sent["view"], _ApprovalView)
    # Mentions inside model-written arguments must not ping anyone.
    assert sent["allowed_mentions"] is not None


async def test_a_channel_question_is_confirmed_by_direct_message() -> None:
    """The answer is public; the question about changing something is not."""
    user = FakeUser(ALICE_ID)
    surface: ConfirmationSurface = DirectMessageConfirmation(user)  # type: ignore[arg-type]

    await asyncio.wait_for(surface.ask(a_prompt(), 0.05), timeout=5)

    assert len(user.sent) == 1
    assert isinstance(user.sent[0]["view"], _ApprovalView)


async def test_closed_direct_messages_make_the_prompt_undeliverable() -> None:
    """Never the fallback of asking the channel instead."""
    user = FakeUser(ALICE_ID, forbidden=True)
    surface: ConfirmationSurface = DirectMessageConfirmation(user)  # type: ignore[arg-type]

    with pytest.raises(Undeliverable):
        await surface.ask(a_prompt(), 0.05)


async def test_the_view_is_bound_to_the_prompts_requester_not_the_recipient() -> None:
    """Who may approve is settled by the prompt, not by who the DM reached."""
    user = FakeUser(MALLORY_ID)
    surface: ConfirmationSurface = DirectMessageConfirmation(user)  # type: ignore[arg-type]

    await asyncio.wait_for(surface.ask(a_prompt(requester=ALICE), 0.05), timeout=5)

    view = user.sent[0]["view"]
    assert await view.interaction_check(FakeInteraction(MALLORY_ID)) is False


# --- the wiring --------------------------------------------------------


async def test_the_bot_hands_a_private_surface_to_every_question() -> None:
    """Driven, not inspected: a surface supplied only sometimes is absent.

    Which tools a run reaches is decided inside the run, so the adapter cannot
    know in advance whether this question will want to change something. It
    supplies the channel for all of them.
    """
    asks = RecordingAskService()
    client = CyberFriendClient(asks, GUILD_ID)  # type: ignore[arg-type]

    await client.on_message(FakeMessage(ALICE_ID, "close issue 42", guild=None))

    assert len(asks.calls) == 1
    request, surface = asks.calls[0]
    assert request.asker == ALICE
    assert isinstance(surface, DirectMessageConfirmation)


async def test_a_bots_own_message_never_opens_a_confirmation_channel() -> None:
    asks = RecordingAskService()
    client = CyberFriendClient(asks, GUILD_ID)  # type: ignore[arg-type]

    await client.on_message(FakeMessage(ALICE_ID, "close issue 42", guild=None, bot=True))

    assert asks.calls == []


async def test_the_ask_service_attends_the_askers_own_channel() -> None:
    """The channel the run finds is bound to the person who asked, and to them only."""
    seen: list[object] = []

    class Peeking:
        async def answer(self, question: Question) -> Answer:
            seen.append(current_channel())
            return Answer(text="ok")

    surface = RecordingSurface()
    service = ask_service(Peeking(), desk=ConfirmationDesk(ConfirmationLedger()))

    await service.ask(a_request(), surface)  # type: ignore[arg-type]

    channel = seen[0]
    assert channel is not None
    assert channel.requester == ALICE  # type: ignore[attr-defined]
    assert channel.surface is surface  # type: ignore[attr-defined]


async def test_the_channel_is_taken_down_when_the_ask_ends() -> None:
    """A confirmation cannot be collected for a run that is already over."""
    service = ask_service(Answering(), desk=ConfirmationDesk(ConfirmationLedger()))

    await service.ask(a_request(), RecordingSurface())  # type: ignore[arg-type]

    assert current_channel() is None


async def test_a_deployment_with_no_desk_attends_nothing() -> None:
    """Unable to ask is the safe half, and the half that survives no wiring."""
    seen: list[object] = []

    class Peeking:
        async def answer(self, question: Question) -> Answer:
            seen.append(current_channel())
            return Answer(text="ok")

    service = ask_service(Peeking(), desk=None)

    await service.ask(a_request(), RecordingSurface())  # type: ignore[arg-type]

    assert seen == [None]


async def test_a_run_that_asks_reaches_the_surface_the_adapter_supplied() -> None:
    """The whole chain, in the shape production uses it.

    An answer service that wants to change something calls `seek_confirmation`
    the way a federated invocation would, and the prompt comes out of the
    surface the Discord adapter handed in at the top. If any link between the
    two is missing this is the test that says so.
    """
    surface = RecordingSurface(reply=ConfirmationReply(ALICE, granted=True))
    ledger = ConfirmationLedger()

    class Mutating:
        async def answer(self, question: Question) -> Answer:
            verdict = await seek_confirmation(a_prompt())
            return Answer(text=str(verdict))

    service = ask_service(Mutating(), desk=ConfirmationDesk(ledger))

    outcome = await service.ask(a_request(), surface)  # type: ignore[arg-type]

    assert surface.seen, "the prompt never reached the person"
    assert outcome.scoped is not None
    assert outcome.scoped.answer.text == "granted"
    # And the grant landed in the ledger the invoke-time gate reads, for
    # exactly the call that was shown.
    assert ledger.state_for(_a_matching_request()) is ConfirmationState.GRANTED


# --- fakes -------------------------------------------------------------


@dataclass
class RecordingSurface:
    """A private surface that remembers what it was asked to show."""

    reply: ConfirmationReply | None = None
    seen: list[ConfirmationPrompt] = field(default_factory=list)

    async def ask(
        self, prompt: ConfirmationPrompt, window_seconds: float
    ) -> ConfirmationReply | None:
        self.seen.append(prompt)
        return self.reply


class Answering:
    async def answer(self, question: Question) -> Answer:
        return Answer(text="ok")


@dataclass
class RecordingAskService:
    """Stands in for the use case, to see exactly what the adapter hands it."""

    calls: list[tuple[AskRequest, object]] = field(default_factory=list)

    async def ask(self, request: AskRequest, confirm: object = None, **_: object) -> object:
        self.calls.append((request, confirm))
        return SimpleOutcome()


@dataclass
class SimpleOutcome:
    rate_limited: bool = False
    retry_after_seconds: float = 0.0
    scoped: Any = None
    alert: Any = None
    suggestion: Any = None

    def __post_init__(self) -> None:
        from chatmemory.app.disclosure import ScopedAnswer

        self.scoped = ScopedAnswer(answer=Answer(text="ok"), withheld_from_audience=frozenset())


class FakeResponse:
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self._sink.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs: Any) -> None:
        self._sink.append(kwargs)


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, content: str, **kwargs: Any) -> FakeSentMessage:
        self.sent.append({"content": content, **kwargs})
        return FakeSentMessage()


class FakeInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = FakeUser(user_id)
        self.sent: list[dict[str, Any]] = []
        self.response = FakeResponse(self.sent)
        self.followup = FakeFollowup()


class FakeSentMessage:
    """What a delivered prompt comes back as: something that can be redrawn."""

    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


class FakeUser:
    def __init__(self, user_id: int, forbidden: bool = False) -> None:
        self.id = user_id
        self._forbidden = forbidden
        self.sent: list[dict[str, Any]] = []

    async def send(self, content: str, **kwargs: Any) -> FakeSentMessage:
        if self._forbidden:
            raise discord.Forbidden(_a_403(), "cannot send messages to this user")
        self.sent.append({"content": content, **kwargs})
        return FakeSentMessage()


class FakeChannelSink:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.name = "general"

    def typing(self) -> Any:
        class _Typing:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Typing()


class FakeMessage:
    def __init__(
        self, author_id: int, content: str, guild: object = None, bot: bool = False
    ) -> None:
        self.author = FakeAuthor(author_id, bot)
        self.content = content
        self.guild = guild
        self.channel = FakeChannelSink(CHANNEL_ID)
        self.mentions: list[object] = []
        self.replies: list[str] = []

    async def reply(self, content: str, **kwargs: Any) -> None:
        self.replies.append(content)


class FakeAuthor(FakeUser):
    def __init__(self, user_id: int, bot: bool) -> None:
        super().__init__(user_id)
        self.bot = bot


def _a_403() -> Any:
    class _Response:
        status = 403
        reason = "Forbidden"

    return _Response()


async def press(view: _ApprovalView, label: str, interaction: FakeInteraction) -> None:
    """Click the button by its label, through discord.py's own dispatch.

    The class attribute is a template; the live item is the copy in
    `children`, and pressing that is what a person actually does.
    """
    button = next(
        i
        for i in view.children
        if isinstance(i, discord.ui.Button) and i.label == label
    )
    await button.callback(interaction)  # type: ignore[arg-type]


# --- a real AskService over fakes --------------------------------------


class FixedAcl:
    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        return Viewer(person=person, visible_channels=frozenset({ChannelRef("discord", 1)}))


class FixedAudiences:
    async def resolve_private(self, person: PersonRef) -> Audience:
        return Audience(
            mode=DeliveryMode.DIRECT_MESSAGE,
            members=frozenset({person}),
            readable_channels=frozenset({ChannelRef("discord", 1)}),
        )

    async def resolve_for_channel(self, channel: ChannelRef) -> Audience:
        return await self.resolve_private(PersonRef("discord", 0))


def ask_service(answers: object, desk: ConfirmationDesk | None) -> AskService:
    return AskService(
        acl=FixedAcl(),  # type: ignore[arg-type]
        audiences=FixedAudiences(),  # type: ignore[arg-type]
        answers=answers,  # type: ignore[arg-type]
        limiter=RateLimiter(),
        desk=desk,
    )


def a_request() -> AskRequest:
    return AskRequest(asker=ALICE, text="close issue 42", destination=None, location_id=1)


def _a_matching_request() -> Any:
    from chatmemory.app.authorization import ActionOrigin, InvocationRequest

    return InvocationRequest(
        requester=ALICE,
        question="close issue 42",
        qualified_name="issues:close",
        arguments=ARGUMENTS,
        origin=ActionOrigin.REQUESTER_REQUEST,
    )
