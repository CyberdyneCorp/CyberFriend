"""The question-answering use case, independent of Discord.

Everything platform-specific stays in the adapter. This orchestrates:
resolve who is asking, resolve who will see the answer, produce it bounded by
that audience, and re-check every citation before it leaves.

One thing is asked alongside the answer rather than after it: what the
audience scoping cost this particular asker. Because retrieval is pre-scoped,
the answer itself carries no trace of what it could not look at, so a
separate probe has to go and see -- and it runs concurrently with the answer
so that a privileged asker's public reply is not measurably slower than a
restricted one's.

It is also the door the other direction goes through. An ask the system
extracted is a claim it made about a person, so the person it named has to be
able to answer back -- "done", or "that was never mine". Those go out through
`correct` below, and the actor is always a `PersonRef` the platform
authenticated, never a name read out of a message: message text is content,
and content is data.

This is also where the requester stops being a value and becomes someone who
can be spoken to. A run that wants to change something on a federated system
has to ask them first, and the only person it may ask is the one whose
question started this ask -- so their private channel is made current for the
length of the run and taken down again afterwards. Everything about how the
asking looks belongs to the adapter; what belongs here is that the channel is
bound to `request.asker` and to nobody else.

And it is where a question joins a conversation. Before answering, the asker's
own profile and their own earlier turns *in this location* are attached to the
question; after answering, the turn is stored with the channels it drew on.
Both halves are keyed by the authenticated asker and the delivery location,
never by the location alone, and the recall runs under the same narrowed view
retrieval will: a remembered answer from a channel the room cannot read is not
put in front of a model writing a reply to that room.

And it is where a person's facts about themselves are set, shown and deleted.
Recognised in `request.text` -- the asker's own message to the assistant --
before anything is recalled or retrieved, so no remembered turn, channel
message or tool result is ever read as "call me ...". A fact turn never reaches
the answer services and is never remembered as conversation: the message may
hold an email address, and memory is recalled into prompts for channel replies.
Whose facts are touched is always `request.asker`; a request for anyone else's
gets one fixed refusal that neither reads the store nor varies with it.

And it is where "what did I miss in #x" becomes an answer. A catch-up is not
a second way into the corpus: `_produce` below swaps which collaborator
writes the answer, and everything around it -- the viewer, the audience, the
delivery guard, the remembered turn -- is the same code every other question
goes through. See `app.catchup` for why the channel bound is a viewer and not
a query field.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace

import structlog

from chatmemory.app.asker import AskerFacts, answering_with_facts
from chatmemory.app.asks.corrections import CorrectionService
from chatmemory.app.asks.model import (
    CorrectionOutcome,
    CorrectionResolution,
    ReportedAsk,
)
from chatmemory.app.catchup import CatchUpService, catch_up_request
from chatmemory.app.confirmation import (
    ConfirmationChannel,
    ConfirmationDesk,
    ConfirmationSurface,
    attending,
)
from chatmemory.app.conversation import Conversations
from chatmemory.app.disclosure import ScopedAnswer, WithheldEvidenceProbe, enforce_audience
from chatmemory.app.facts import (
    DIRECT_ONLY_KINDS,
    FactOutcome,
    FactResult,
    PersonalFactsService,
)
from chatmemory.app.limits import RateLimiter
from chatmemory.app.routing import FactAction, FactIntent, fact_intent, indexing_request
from chatmemory.domain.audience import Audience
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.acl import AclResolver, AudienceResolver
from chatmemory.ports.answers import (
    Answer,
    AnswerService,
    AskerProfile,
    AskerProfileResolver,
    Question,
)
from chatmemory.ports.facts import (
    MAX_FULL_NAME_CHARS,
    MAX_PREFERRED_NAME_CHARS,
    FactKind,
    FactRejection,
    PersonalFacts,
)
from chatmemory.ports.memory import ConversationLocation, MemoryPurge, Recollection

log = structlog.get_logger()

INDEXING_POINTER = (
    "I can't change which channels are indexed from a chat message. Someone with "
    "Manage Channels on the channel can use `/index` or `/unindex` there; Discord "
    "decides who that is, not what the message says."
)

FACT_LABELS = {
    FactKind.PREFERRED_NAME: "preferred name",
    FactKind.EMAIL: "email address",
    FactKind.PREFERRED_LANGUAGE: "preferred language",
    FactKind.PHONE: "phone number",
    FactKind.ETH_WALLET: "Ethereum wallet",
    FactKind.BTC_WALLET: "Bitcoin wallet",
    FactKind.FULL_NAME: "full name",
}

REMEMBERABLE = (
    "I can remember these about you, if you tell me yourself:\n"
    "- your full name (`my name is Leonardo Araujo`)\n"
    "- the name you'd like me to call you (`call me Leo`)\n"
    "- your email address (`my email is ...`)\n"
    "- your phone number (`my phone is ...`)\n"
    "- the language you'd like answers in (`reply to me in Portuguese`)\n"
    "- your Ethereum wallet (`my wallet is 0x...`) and your Bitcoin wallet "
    "(`my btc wallet is ...`)\n"
    "You can tell me several at once. "
    "Ask `what do you know about me?` to see them, or `forget my email` to "
    "delete one. Once I have your wallet, `what's my balance?` uses it.\n"
    "Your email, phone and wallets I only ever show you, in a direct message."
)

FACT_UNSUPPORTED = "I haven't saved that. " + REMEMBERABLE

# One answer for "that's about someone else", whoever it names.
FACT_ABOUT_SOMEONE_ELSE = (
    "I only remember what people tell me about themselves, so I haven't saved "
    "that. They can tell me directly."
)

# One answer for "what is X's email?", whether or not X ever set one. It is
# returned without reading the store, so neither its words nor its timing can
# say whether anything is there.
FACT_OTHERS_REFUSED = (
    "I don't share anything people have told me about themselves, and I can't "
    "say whether they have. Ask them directly."
)

FACTS_UNAVAILABLE = "I can't remember personal details on this deployment."

# Said in every channel reply about facts, whether or not an email is stored:
# a note that appeared only when there was one would announce that there is.
EMAIL_CHANNEL_NOTE = "-# I only show an email address in a direct message to its owner."

FACT_REJECTIONS = {
    FactRejection.EMPTY: "it was empty",
    FactRejection.TOO_LONG: "it's too long",
    FactRejection.MALFORMED: "it isn't the right shape",
    FactRejection.DISALLOWED_CHARACTERS: (
        "it can only use letters, numbers, spaces and simple punctuation"
    ),
}

FACT_NOT_STORED = (
    "I didn't save that: you've opted out, so I don't keep personal details for you."
)


def _stored_reply(result: FactResult, direct: bool) -> str:
    fact = result.fact
    assert fact is not None
    if fact.kind is FactKind.PREFERRED_NAME:
        return f"Got it, I'll call you **{fact.value}**."
    if fact.kind is FactKind.FULL_NAME:
        return f"Got it, your full name is **{fact.value}**."
    if fact.kind is FactKind.PREFERRED_LANGUAGE:
        return f"Got it, I'll answer you in **{fact.value}**."
    # Named by its own label: a phone or wallet was once confirmed as "your
    # email address".
    label = FACT_LABELS[fact.kind]
    if direct:
        return (
            f"Got it, I've saved your {label} as `{fact.value}`. I only "
            "show it to you, in a direct message."
        )
    # Confirmed without the value: a channel reply is read by everyone here.
    return (
        f"Got it, I've saved your {label}. I only show it to you in a "
        "direct message, so I won't repeat it here."
    )


MALFORMED_BY_KIND = {
    FactKind.EMAIL: "it isn't a well-formed email address",
    FactKind.PHONE: "it doesn't look like a phone number",
    FactKind.ETH_WALLET: (
        "it isn't a well-formed address - I expect `0x` and 40 hex characters"
    ),
    FactKind.BTC_WALLET: (
        "it isn't a well-formed Bitcoin address - I expect one starting `1`, "
        "`3` or `bc1`"
    ),
}
"""Why a value was the wrong shape, said per kind.

A generic "it isn't the right shape" tells somebody nothing they can act on,
and the shapes differ enough to be worth naming: a person who typed a Bitcoin
address where their Ethereum wallet goes needs to know which was expected."""


def fact_set_reply(result: FactResult, direct: bool) -> str:
    """What the person is told after asking to set a fact.

    Every stored fact is confirmed back, so a misrecognised "call me ..." is
    visible the moment it happens. A refused value is never repeated: an
    almost-email is still personal data.
    """
    label = FACT_LABELS[result.kind]
    if result.outcome is FactOutcome.STORED:
        return _stored_reply(result, direct)
    if result.outcome is FactOutcome.NOT_STORED:
        return FACT_NOT_STORED
    return f"I didn't save that as your {label}: {_rejection_reason(result)}."


def _rejection_reason(result: FactResult) -> str:
    """Why a value was refused, without repeating it."""
    rejection = result.rejection or FactRejection.MALFORMED
    reason = (
        MALFORMED_BY_KIND.get(result.kind, FACT_REJECTIONS[FactRejection.MALFORMED])
        if rejection is FactRejection.MALFORMED
        else FACT_REJECTIONS.get(rejection, "")
    )
    limits = {
        FactKind.PREFERRED_NAME: MAX_PREFERRED_NAME_CHARS,
        FactKind.FULL_NAME: MAX_FULL_NAME_CHARS,
    }
    limit = (
        f" (at most {limits[result.kind]} characters)"
        if result.rejection is FactRejection.TOO_LONG and result.kind in limits
        else ""
    )
    return f"{reason}{limit}"


def facts_set_reply(
    results: Sequence[FactResult], not_kept: Sequence[str], direct: bool
) -> str:
    """One reply for an introduction: what was saved, what was not, and why.

    Values follow the same rule as a single fact: shown back in a direct
    message, withheld in a channel for the kinds only ever shown to their
    owner, and never repeated when refused.
    """
    lines: list[str] = []
    for result in results:
        label = FACT_LABELS[result.kind].capitalize()
        if result.outcome is FactOutcome.NOT_STORED:
            return FACT_NOT_STORED
        if result.outcome is FactOutcome.REJECTED:
            lines.append(f"✗ {label}: not saved, {_rejection_reason(result)}")
            continue
        assert result.fact is not None
        hidden = not direct and result.kind in DIRECT_ONLY_KINDS
        lines.append(
            f"✓ {label}: saved (shown only in a direct message)" if hidden
            else f"✓ {_shown_line(result.kind, result.fact.value).removeprefix('• ')}"
        )
    lines.extend(f"✗ {kind.capitalize()}: I don't keep that" for kind in not_kept)
    return "\n".join(["Here's what I saved:", *lines])


def facts_shown_reply(facts: PersonalFacts, direct: bool) -> str:
    """The person's own facts, as they may be shown where they asked.

    `facts` has already been through `visible_facts`, so a channel reply has no
    email to show. The wording must not let "not shown here" read as "not
    stored": the channel version never says there is nothing.
    """
    lines = [_shown_line(stored.fact.kind, stored.fact.value) for stored in facts.facts]
    if direct:
        if not lines:
            return "You haven't asked me to remember anything about you.\n\n" + REMEMBERABLE
        return "\n".join(["Here's what you've asked me to remember:", *lines])
    head = (
        "Here's what I can show you here:"
        if lines
        else "I have no preferred name or language saved for you."
    )
    return "\n".join([head, *lines, EMAIL_CHANNEL_NOTE])


def _shown_line(kind: FactKind, value: str) -> str:
    # Values were validated to carry no markdown, so they are safe to embolden;
    # an email goes in code so its underscores are not read as emphasis.
    shown = f"`{value}`" if kind is FactKind.EMAIL else f"**{value}**"
    return f"• {FACT_LABELS[kind].capitalize()}: {shown}"


def fact_forgotten_reply(kind: FactKind | None) -> str:
    """The same words whether or not there was anything to delete.

    In a channel, "you had no email saved" would tell the room something; and
    the person's goal -- that it is gone -- holds either way.
    """
    if kind is None:
        return (
            "Done. I don't have a preferred name, email address or preferred "
            "language for you any more."
        )
    return f"Done. I don't have a {FACT_LABELS[kind]} for you any more."


@dataclass(frozen=True, slots=True)
class AskRequest:
    """A question, and where its answer will be delivered.

    `destination` is None for a direct message. The delivery target -- not
    anything in the question text -- determines the audience.
    """

    asker: PersonRef
    text: str
    destination: ChannelRef | None
    location_id: int

    @property
    def location(self) -> ConversationLocation:
        """The conversation this question belongs to, for this asker.

        A direct message is part of the key, not a label on it: a DM answer
        was scoped to one person and a channel answer to everyone present.
        """
        return conversation_location(
            self.asker, self.location_id, direct=self.destination is None
        )


def conversation_location(
    asker: PersonRef, location_id: int, *, direct: bool
) -> ConversationLocation:
    """One place, spelled the same way by asking and by forgetting.

    `/forget here` must name exactly the location `/ask` remembered under, or
    it reports success over a conversation it never touched.
    """
    return ConversationLocation(asker.platform, location_id, direct=direct)


@dataclass(frozen=True, slots=True)
class ForgetRequest:
    """A person asking to erase their own conversation.

    `requester` comes from the platform event, like every actor here; there is
    no field naming whose history to delete, because the only history anyone
    may erase is their own. `location` None means everywhere.
    """

    requester: PersonRef
    location: ConversationLocation | None


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    """One person's statement about one of their own asks.

    `actor` is carried as a `PersonRef` rather than a name or a viewer because
    of where it has to come from: the platform event that delivered the click.
    A surface that read the actor out of message text would be taking identity
    from content, which is the one thing content may never be.

    There is no viewer field, and there must not be one. Which asks this person
    may see, and which of those are theirs to close, is resolved here from
    `actor` and bound into the store's predicate -- so correcting somebody
    else's obligation is not expressible.
    """

    actor: PersonRef
    ask_key: str
    resolution: CorrectionResolution


@dataclass(frozen=True, slots=True)
class AskOutcome:
    scoped: ScopedAnswer | None
    rate_limited: bool = False
    retry_after_seconds: float = 0.0

    @property
    def answered(self) -> bool:
        return self.scoped is not None


class AskService:
    def __init__(
        self,
        acl: AclResolver,
        audiences: AudienceResolver,
        answers: AnswerService,
        limiter: RateLimiter,
        withheld: WithheldEvidenceProbe | None = None,
        desk: ConfirmationDesk | None = None,
        corrections: CorrectionService | None = None,
        conversations: Conversations | None = None,
        profiles: AskerProfileResolver | None = None,
        facts: PersonalFactsService | None = None,
        catchup: CatchUpService | None = None,
    ) -> None:
        self._acl = acl
        self._audiences = audiences
        self._answers = answers
        self._limiter = limiter
        # Optional so a surface under test answers exactly as a first question
        # would. The bot process passes both, and `test_memory_integration`
        # reads that chain from the entrypoint down: an optional collaborator
        # is only honest if something proves the running process supplies it.
        self._conversations = conversations
        self._profiles = profiles
        # Optional like memory, and proven wired the same way:
        # `test_facts_behaviour` reads the chain from the entrypoint down.
        # Without it a fact request is answered that facts are unavailable,
        # never searched for in the corpus.
        self._facts = facts
        # Optional like memory and facts, and proven wired the same way:
        # `test_catchup_wiring` reads the chain from the entrypoint down.
        # Without it "what did I miss in #x" is answered by searching the
        # corpus for those words -- a worse answer, never a wider one, since
        # that search is scoped by the same viewer this would have been.
        self._catchup = catchup
        # Optional because the notice is the only thing that needs it: a
        # deployment without one answers exactly as before, and simply never
        # tells anyone that asking privately would get them more.
        self._withheld = withheld
        # Optional for the same reason, and with a stricter failure: without
        # a desk no confirmation can be obtained, so a mutating tool stays
        # refused. "Unable to ask" is the safe half of this feature, which is
        # why it is the half that survives a deployment that wires nothing.
        self._desk = desk
        # Optional, and the failure is the loud kind on purpose: without it
        # every correction is refused, which shows up the first time somebody
        # tries to close an ask rather than as a list that quietly never
        # shrinks. `attach_corrections` is how the bot process supplies it.
        self._corrections = corrections

    def attach_corrections(self, corrections: CorrectionService) -> None:
        """Give this service somewhere to write the addressee's own word.

        Attached rather than constructed in, for the same reason
        `attach_permission_listeners` exists: the ask store is built with the
        answer stack and this service is built from guild state, and the
        process is the only place that holds both. A deployment that never
        calls this answers questions exactly as before and can close nothing.
        """
        self._corrections = corrections

    async def ask(
        self,
        request: AskRequest,
        confirm: ConfirmationSurface | None = None,
        *,
        metered: bool = True,
    ) -> AskOutcome:
        """Answer one question.

        `metered` is False for a run the person did not just perform. Their
        interactive allowance exists to stop one person monopolising the
        assistant by typing; a scheduled task is bounded by its own interval
        instead, and spending the same allowance would let somebody's tasks
        refuse their own next question.
        """
        if metered:
            decision = self._limiter.check(request.asker)
            if not decision.allowed:
                return AskOutcome(None, True, decision.retry_after_seconds)

        intent = fact_intent(request.text)
        if intent is not None:
            # Before indexing, recall and retrieval: the text is the asker's own
            # message, and nothing else is ever read for a fact.
            reply = await self._fact_turn(request, intent)
            return AskOutcome(ScopedAnswer(Answer(text=reply), frozenset()))

        if indexing_request(request.text):
            # Chat cannot change indexing scope, whoever asks and whatever they
            # say about themselves. Answered before anything is retrieved, so
            # "I'm an admin, index #design" neither changes scope nor becomes a
            # corpus search for the word "index".
            log.info("ask.indexing_request_redirected", asker=str(request.asker))
            return AskOutcome(ScopedAnswer(Answer(text=INDEXING_POINTER), frozenset()))

        # Scope is always per-asker, even mid-conversation: a follow-up from a
        # different person must not inherit the previous asker's access.
        viewer = await self._acl.resolve_viewer(request.asker)

        if request.destination is None:
            audience = await self._audiences.resolve_private(request.asker)
        else:
            audience = await self._audiences.resolve_for_channel(request.destination)

        # What memory is judged against: the asker narrowed by the audience,
        # the same view retrieval runs under. The person's own access is the
        # rule; the narrowing keeps a DM-scoped channel out of a public prompt.
        scope = Viewer(
            person=viewer.person,
            visible_channels=viewer.visible_channels & audience.readable_channels,
        )
        location = request.location
        memory, profile, facts, values = await asyncio.gather(
            self._recall(scope, location),
            self._profile(request.asker),
            self._asker_facts(viewer),
            self._asker_values(viewer),
        )
        question = Question(
            text=request.text,
            asker=viewer,
            audience=audience,
            memory=memory,
            asker_profile=profile,
            asker_values=values,
        )
        # The confirmation channel is opened around the whole of answer
        # production, and closed the moment it ends: a confirmation cannot be
        # collected for a run that is already over, and the next asker gets
        # their own. `gather` inside `_produce` copies the current context
        # into each task it starts, so a tool call made deep inside the
        # answer finds this channel and no other.
        with self._attending(request.asker, confirm), answering_with_facts(facts):
            answer, withheld = await self._produce(request, question, viewer, audience)
        scoped = enforce_audience(answer, audience, viewer, withheld_by_scoping=withheld)

        await self._remember(scope, location, request.text, answer, scoped, memory)

        log.info(
            "ask.answered",
            asker=str(request.asker),
            mode=audience.mode,
            citations=len(scoped.answer.citations),
            withheld=len(scoped.withheld_from_audience),
            abstained=scoped.answer.abstained,
            remembered_turns=len(memory.turns),
            remembered_summaries=len(memory.summaries),
        )
        return AskOutcome(scoped)

    async def _produce(
        self,
        request: AskRequest,
        question: Question,
        viewer: Viewer,
        audience: Audience,
    ) -> tuple[Answer, frozenset[ChannelRef]]:
        """The answer, and what audience scoping cost this asker.

        Two ways to produce one answer, not two answer paths. A catch-up is
        the ordinary retrieval and the ordinary synthesiser with the viewer
        pinned to one channel and the query pinned to a period, so what comes
        back is an `Answer` like any other -- cited, audience-checked below,
        and remembered as a turn. A deployment that wires no catch-up service
        answers the question by searching the corpus, exactly as before.

        No withheld-evidence probe for a catch-up, and that is not an
        omission. The probe asks "is there more about this question in
        channels the room cannot read", and for a catch-up the answer is a
        statement about the one channel named -- which is the very thing the
        refusal above declines to disclose.
        """
        catch_up = catch_up_request(request.text)
        if catch_up is not None and self._catchup is not None:
            log.info(
                "ask.catch_up",
                asker=str(request.asker),
                period_named=catch_up.period_named,
                named_a_channel=catch_up.named_a_channel,
            )
            summary = await self._catchup.summarise(question, catch_up, request.destination)
            return summary, frozenset()
        # Concurrent, and not merely for speed. Run after the answer, the
        # probe would add its latency only for askers who have channels the
        # room does not -- which makes "this person can see more than you" a
        # property of how long the bot took to reply.
        answer, withheld = await asyncio.gather(
            self._answers.answer(question),
            self._withheld_channels(viewer, audience, request.text),
        )
        return answer, withheld

    async def forget(self, request: ForgetRequest) -> MemoryPurge | None:
        """Erase the requester's own conversation, here or everywhere.

        None when this deployment keeps no memory, so a surface can say so
        rather than report a deletion of nothing as a success.
        """
        if request.location is None and self._facts is not None:
            # Facts belong to the person, not to a location, so only the
            # unscoped forget reaches them. Not caught: "forgot everything"
            # that kept an email address must fail loudly, not report success.
            await self._facts.forget_all(await self._acl.resolve_viewer(request.requester))
        if self._conversations is None:
            return None
        purge = await self._conversations.forget(request.requester, request.location)
        log.info(
            "ask.forgotten",
            requester=str(request.requester),
            everywhere=request.location is None,
            turns=purge.turns,
            summaries=purge.summaries,
        )
        return purge

    async def _recall(self, scope: Viewer, location: ConversationLocation) -> Recollection:
        if self._conversations is None:
            return Recollection()
        return await self._conversations.recall(scope, location)

    async def _fact_turn(self, request: AskRequest, intent: FactIntent) -> str:
        """Set, show or delete the asker's own facts, and say what happened."""
        log.info(
            "ask.fact_intent",
            asker=str(request.asker),
            action=intent.action.value,
            kind=intent.kind.value if intent.kind else None,
            direct=request.destination is None,
        )
        # Refusals first, and before the wiring check: whether a deployment
        # keeps facts is no reason to answer a question about someone else
        # differently.
        if intent.action is FactAction.OTHERS_FACTS:
            return FACT_OTHERS_REFUSED
        if intent.action is FactAction.ABOUT_SOMEONE_ELSE:
            return FACT_ABOUT_SOMEONE_ELSE
        if intent.action is FactAction.UNSUPPORTED:
            return FACT_UNSUPPORTED
        if self._facts is None:
            return FACTS_UNAVAILABLE
        # The viewer is resolved from the authenticated asker, and it is the
        # only key the fact service takes: there is no way to name anyone else.
        viewer = await self._acl.resolve_viewer(request.asker)
        direct = request.location.direct
        if intent.action is FactAction.SHOW:
            return facts_shown_reply(await self._facts.facts_for(viewer, request.location), direct)
        if intent.action is FactAction.FORGET:
            if intent.kind is None:
                await self._facts.forget_all(viewer)
            else:
                await self._facts.forget(viewer, intent.kind)
            return fact_forgotten_reply(intent.kind)
        if intent.action is FactAction.SET_MANY:
            results = [await self._facts.remember(viewer, k, v) for k, v in intent.sets]
            return facts_set_reply(results, intent.not_kept, direct)
        assert intent.kind is not None and intent.value is not None
        result = await self._facts.remember(viewer, intent.kind, intent.value)
        return fact_set_reply(result, direct)

    #: Facts whose value may become an outbound argument for their owner.
    #: Only the chain addresses: a name or a language is not something any
    #: tool takes, and an email or a phone number must never leave at all.
    OUTBOUND_KINDS = (FactKind.ETH_WALLET, FactKind.BTC_WALLET)

    async def _asker_values(self, viewer: Viewer) -> frozenset[str]:
        """The asker's own addresses, for the egress guard and nothing else.

        A second read, deliberately not `_asker_facts`. That one is filtered as
        a channel would see it so a prompt can never hold a private value; this
        one is unfiltered because the guard needs the exact stored string to
        compare against, and its result never reaches a prompt, an answer or a
        log line.
        """
        if self._facts is None:
            return frozenset()
        try:
            stored = await self._facts.facts_for(
                viewer, ConversationLocation(viewer.person.platform, 0, direct=True)
            )
        except Exception:
            # No values is never a failure: the lookup then asks for an address.
            log.exception("ask.values_failed", asker=str(viewer.person))
            return frozenset()
        return frozenset(
            value for kind in self.OUTBOUND_KINDS if (value := stored.get(kind))
        )

    async def _asker_facts(self, viewer: Viewer) -> AskerFacts | None:
        """The asker's name and language for the prompt. Never their email."""
        if self._facts is None:
            return None
        try:
            # Read as a channel would see them, so the email is dropped by the
            # same filter every channel reply goes through -- even for a direct
            # message. Nothing an answer needs depends on it, and a prompt that
            # never held it cannot leak it.
            stored = await self._facts.facts_for(
                viewer, ConversationLocation(viewer.person.platform, 0, direct=False)
            )
        except Exception:
            # No facts is never a failure: the question is answered without them.
            log.exception("ask.facts_failed", asker=str(viewer.person))
            return None
        return AskerFacts(
            person=viewer.person,
            preferred_name=stored.get(FactKind.PREFERRED_NAME),
            full_name=stored.get(FactKind.FULL_NAME),
            preferred_language=stored.get(FactKind.PREFERRED_LANGUAGE),
        )

    async def _profile(self, asker: PersonRef) -> AskerProfile | None:
        """The asker's own profile. Only ever looked up for `request.asker`."""
        if self._profiles is None:
            return None
        try:
            return await self._profiles.resolve_profile(asker)
        except Exception:
            # The port says it must not raise; this is the boundary that holds
            # if an implementation forgets. No profile is never a failure.
            log.exception("ask.profile_failed", asker=str(asker))
            return None

    async def _remember(
        self,
        scope: Viewer,
        location: ConversationLocation,
        text: str,
        produced: Answer,
        scoped: ScopedAnswer,
        shown: Recollection,
    ) -> None:
        """Store the turn: the text that was delivered, the provenance that was used.

        Delivered text, because a reply the audience guard suppressed must not
        come back as memory in its unsuppressed form. Provenance from what was
        produced, before the guard dropped any citation: a dropped citation was
        still evidence the model read. And from `shown`, the exact recollection
        put in `Question.memory`: the answer may restate it, so the new turn
        must stop being recalled when any channel behind it does.
        """
        if self._conversations is None:
            return
        await self._conversations.remember(
            scope,
            location,
            text,
            replace(
                scoped.answer,
                citations=produced.citations,
                consulted_channels=produced.consulted_channels,
            ),
            informed_by=shown,
        )

    async def correctable(self, person: PersonRef, limit: int = 25) -> Sequence[ReportedAsk]:
        """The asks `person` could close, scoped to what `person` may read.

        The viewer is resolved here from the authenticated actor and is never
        defaulted, so a surface cannot offer somebody a list of another
        person's obligations to pick from.
        """
        if self._corrections is None:
            return ()
        viewer = await self._acl.resolve_viewer(person)
        return await self._corrections.correctable(viewer, limit=limit)

    async def correct(self, request: CorrectionRequest) -> CorrectionOutcome:
        """Record the actor's own correction, or refuse without saying why.

        `UNKNOWN_ASK` and `NOT_ADDRESSEE` are two outcomes for the caller's
        logs, not two things to tell the person. A refusal that distinguished
        "that is not yours" from "there is no such ask" would answer, for any
        key somebody cared to try, whether an ask exists in a channel they
        cannot read -- which is the disclosure the whole read path is arranged
        to prevent.

        An unwired deployment refuses everything, and reports the outcome that
        discloses nothing.
        """
        if self._corrections is None:
            log.warning("ask.correction_unwired", ask_key=request.ask_key)
            return CorrectionOutcome.UNKNOWN_ASK

        viewer = await self._acl.resolve_viewer(request.actor)
        outcome = await self._corrections.correct(
            viewer, request.ask_key, request.resolution
        )
        log.info(
            "ask.corrected",
            actor=str(request.actor),
            resolution=request.resolution,
            outcome=outcome,
        )
        return outcome

    def _attending(
        self, asker: PersonRef, surface: ConfirmationSurface | None
    ) -> AbstractContextManager[None]:
        """Bind the requester's private channel to this run, if there is one.

        Both halves are required. A desk with no surface has nowhere to show
        a prompt; a surface with no desk has no ledger to write the answer
        into. Missing either, nothing attends and every mutating call is
        refused for want of a confirmation -- which is the outcome a
        deployment that has not thought about this should get.
        """
        if self._desk is None or surface is None:
            return nullcontext()
        return attending(ConfirmationChannel(self._desk, surface, asker))

    async def _withheld_channels(
        self, viewer: Viewer, audience: Audience, text: str
    ) -> frozenset[ChannelRef]:
        """What the audience scoping kept from this asker, if anything.

        A private audience is the asker's own visibility, so there is nothing
        the scoping could have withheld and nothing worth a query.
        """
        if self._withheld is None or audience.is_private:
            return frozenset()
        return await self._withheld.withheld_channels(viewer, audience, text)


class StubAnswerService:
    """Placeholder until retrieval lands.

    Abstains rather than inventing an answer, which is the correct behaviour
    for a system with no evidence -- and keeps the surface honest while the
    corpus is being built.
    """

    async def answer(self, question: Question) -> Answer:
        readable = len(question.audience.readable_channels)
        return Answer(
            text=(
                "I can't answer that yet — my message index isn't built. "
                f"When it is, I'll search the {readable} channel(s) everyone here can read."
            ),
            abstained=True,
        )
