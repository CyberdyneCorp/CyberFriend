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

And it is where "tell me when my LP goes out of range" becomes a proposal
rather than a search. Recognised after the asker's facts and earlier turns are
read -- the address may be their saved wallet, or one they typed a few questions
back -- and before either answer path, so an alert request never reaches the
corpus or the positions route. What comes back is the list of what would be
watched, carried on `AskOutcome.alert` for the surface to show with a Confirm
button; nothing is stored until that button is pressed. An alert turn is not
remembered: it is a command, and its reply may name the wallet. A scheduled
run (`metered=False`) is never an alert turn: nobody is there to confirm.

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

from chatmemory.app.alert_intent import ALERT_CREATE, AlertIntent, alert_intent
from chatmemory.app.alert_requests import (
    AlertProposal,
    AlertRequests,
    alert_language,
    alerts_unavailable,
)
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
    PersonalFactsService,
)
from chatmemory.app.limits import RateLimiter
from chatmemory.app.routing import FactAction, FactIntent, fact_intent, indexing_request
from chatmemory.domain.audience import Audience
from chatmemory.domain.chain import is_address, named_by_suffix
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
    FactKind,
)
from chatmemory.ports.memory import ConversationLocation, MemoryPurge, Recollection

log = structlog.get_logger()

INDEXING_POINTER_PT = (
    "Não consigo mudar quais canais são indexados por uma mensagem de chat. "
    "Alguém com Gerenciar Canais no canal pode usar `/index` ou `/unindex` lá; "
    "quem decide isso é o Discord, não o que a mensagem diz."
)
INDEXING_POINTER = (
    "I can't change which channels are indexed from a chat message. Someone with "
    "Manage Channels on the channel can use `/index` or `/unindex` there; Discord "
    "decides who that is, not what the message says."
)

# Reply wording lives in `fact_replies`, in each language; re-exported here
# because these names have always been importable from this module.
from chatmemory.app.fact_replies import (  # noqa: E402
    EMAIL_CHANNEL_NOTE,
    FACT_ABOUT_SOMEONE_ELSE,
    FACT_LABELS,
    FACT_NOT_STORED,
    FACT_OTHERS_REFUSED,
    FACT_REJECTIONS,
    FACT_UNSUPPORTED,
    FACTS_UNAVAILABLE,
    MALFORMED_BY_KIND,
    REMEMBERABLE,
    fact_forgotten_reply,
    fact_set_reply,
    facts_set_reply,
    facts_shown_reply,
    rememberable,
    unsupported,
)
from chatmemory.app.fact_replies import text as fact_text  # noqa: E402
from chatmemory.app.language import Language, detect, language_named  # noqa: E402
from chatmemory.app.localise import localised  # noqa: E402
from chatmemory.app.self_description import (  # noqa: E402
    typed_command,
    typed_command_reply,
)

__all__ = [
    "EMAIL_CHANNEL_NOTE",
    "FACT_ABOUT_SOMEONE_ELSE",
    "FACT_LABELS",
    "FACT_NOT_STORED",
    "FACT_OTHERS_REFUSED",
    "FACT_REJECTIONS",
    "FACT_UNSUPPORTED",
    "FACTS_UNAVAILABLE",
    "MALFORMED_BY_KIND",
    "REMEMBERABLE",
    "fact_forgotten_reply",
    "fact_set_reply",
    "facts_set_reply",
    "facts_shown_reply",
]


def _fact_refusal(action: FactAction, language: Language) -> str | None:
    """The fixed reply for an action that reads and writes nothing, or None."""
    if action is FactAction.OTHERS_FACTS:
        return fact_text("others_refused", language)
    if action is FactAction.ABOUT_SOMEONE_ELSE:
        return fact_text("about_someone_else", language)
    if action is FactAction.UNSUPPORTED:
        return unsupported(language)
    if action is FactAction.CAPABILITIES:
        return rememberable(language)
    return None


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
    #: Alerts to create if the asker confirms; `scoped` holds the same text.
    #: The surface shows it with Confirm and Cancel, answerable by the asker
    #: alone. None for every other reply.
    alert: AlertProposal | None = None

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
        alerts: AlertRequests | None = None,
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
        # Optional, and absent unless alerts are switched on with an Infura
        # key. An alert request is still recognised without it, and answered
        # that alerts are not available here -- never searched for.
        self._alerts = alerts
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
            pointer = (
                INDEXING_POINTER_PT
                if detect(request.text) is Language.PORTUGUESE
                else INDEXING_POINTER
            )
            return AskOutcome(ScopedAnswer(Answer(text=pointer), frozenset()))

        command = typed_command(request.text)
        if command is not None:
            # A slash command sent as text, after the indexing pointer, which
            # says more about /index than "use the menu" does. Answered with how to run it, never
            # searched for: the corpus has no idea what "/forget" does.
            reply = typed_command_reply(command, detect(request.text))
            return AskOutcome(ScopedAnswer(Answer(text=reply), frozenset()))

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
            self._asker_facts(viewer, direct=request.destination is None),
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
        # Before either answer path, and with the asker's own earlier questions:
        # an address typed there is theirs, as one typed here is. Only for
        # somebody present: a proposal needs a Confirm press, and a scheduled
        # run has nobody to press it, so its question is answered as before
        # rather than reading every chain on each run to propose to no one.
        alert = (
            alert_intent(request.text, tuple(turn.question for turn in memory.turns))
            if metered
            else None
        )
        if alert is not None:
            return await self._alert_turn(request, alert, facts, values)
        # The confirmation channel is opened around the whole of answer
        # production, and closed the moment it ends: a confirmation cannot be
        # collected for a run that is already over, and the next asker gets
        # their own. `gather` inside `_produce` copies the current context
        # into each task it starts, so a tool call made deep inside the
        # answer finds this channel and no other.
        with self._attending(request.asker, confirm), answering_with_facts(facts):
            answer, withheld = await self._produce(request, question, viewer, audience)
        scoped = enforce_audience(answer, audience, viewer, withheld_by_scoping=withheld)
        # After scoping, which can itself return the no-answer text.
        scoped = replace(scoped, answer=localised(scoped.answer, detect(request.text)))

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

    async def _alert_turn(
        self,
        request: AskRequest,
        intent: AlertIntent,
        facts: AskerFacts | None,
        values: frozenset[str],
    ) -> AskOutcome:
        """What would be watched, for the asker to confirm; nothing is stored.

        The saved wallets come from `values` -- the exact stored strings -- and
        only the Ethereum addresses among them, as for the positions route.
        """
        language = alert_language(request.text, facts.preferred_language if facts else None)
        direct = request.destination is None
        log.info(
            "ask.alert_request",
            route=ALERT_CREATE,
            asker=str(request.asker),
            kind=str(intent.kind),
            typed=intent.address is not None,
            direct=direct,
        )
        if self._alerts is None:
            reply = alerts_unavailable(language)
            return AskOutcome(ScopedAnswer(Answer(text=reply), frozenset()))
        saved = tuple(sorted(v for v in values if is_address(v)))
        # Several saved: the one the request names by its last characters.
        named = named_by_suffix(request.text, saved) if len(saved) > 1 else ()
        proposed = await self._alerts.propose(
            request.asker,
            intent,
            saved_wallets=named if len(named) == 1 else saved,
            language=language,
            direct=direct,
        )
        answer = Answer(text=proposed.text, consulted_channels=frozenset())
        return AskOutcome(ScopedAnswer(answer, frozenset()), alert=proposed.proposal)

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
        """Set, show or delete the asker's own facts, and say what happened.

        In the language the message was written in: these replies are fixed
        text, so the answer-language rule is applied here rather than by a
        model.
        """
        log.info(
            "ask.fact_intent",
            asker=str(request.asker),
            action=intent.action.value,
            kind=intent.kind.value if intent.kind else None,
            direct=request.destination is None,
        )
        language = detect(request.text)
        # Refusals first, and before the wiring check: whether a deployment
        # keeps facts is no reason to answer a question about someone else
        # differently.
        refused = _fact_refusal(intent.action, language)
        if refused is not None:
            return refused
        if self._facts is None:
            return fact_text("unavailable", language)
        # The viewer is resolved from the authenticated asker, and it is the
        # only key the fact service takes: there is no way to name anyone else.
        viewer = await self._acl.resolve_viewer(request.asker)
        return await self._apply_fact(viewer, request, intent, language)

    async def _apply_fact(
        self, viewer: Viewer, request: AskRequest, intent: FactIntent, language: Language
    ) -> str:
        assert self._facts is not None
        direct = request.location.direct
        if intent.action is FactAction.SHOW:
            facts = await self._facts.facts_for(viewer, request.location)
            return facts_shown_reply(facts, direct, language, intent.kind)
        if intent.action is FactAction.FORGET:
            if intent.kind is None:
                await self._facts.forget_all(viewer)
            else:
                await self._facts.forget(viewer, intent.kind, intent.value)
            return fact_forgotten_reply(
                intent.kind, language, one_value=intent.value is not None
            )
        if intent.action is FactAction.SET_MANY:
            results = [await self._facts.remember(viewer, k, v) for k, v in intent.sets]
            return facts_set_reply(results, intent.not_kept, direct, language)
        assert intent.kind is not None and intent.value is not None
        result = await self._facts.remember(viewer, intent.kind, intent.value)
        return fact_set_reply(result, direct, language)

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
        # Every saved wallet, not the first of each kind: a person with two
        # must be able to ask about either.
        return frozenset(value for kind in self.OUTBOUND_KINDS for value in stored.values(kind))

    async def reply_language(self, asker: PersonRef) -> Language:
        """The language to answer a message with no words in, such as a bare mention.

        The asker's saved preferred language, or UNKNOWN (answered in English).
        A failed read is UNKNOWN too: the reply is a description, not an answer
        anything depends on.
        """
        if self._facts is None:
            return Language.UNKNOWN
        try:
            viewer = await self._acl.resolve_viewer(asker)
            stored = await self._facts.facts_for(
                viewer, ConversationLocation(asker.platform, 0, direct=False)
            )
        except Exception:
            log.exception("ask.reply_language_failed", asker=str(asker))
            return Language.UNKNOWN
        return language_named(stored.get(FactKind.PREFERRED_LANGUAGE))

    async def _asker_facts(self, viewer: Viewer, direct: bool) -> AskerFacts | None:
        """The asker's facts for the prompt, as they may be shown where they asked.

        In a channel, read as a channel would see them: the email, phone and
        wallets are dropped by the same filter every channel reply goes
        through, so a channel prompt never holds them and cannot leak them.

        In a direct message the only reader is their owner, so they are
        included. Before this, even a DM prompt held only the name and
        language, and the assistant could not use what it had been told --
        it answered "I couldn't find anything" to "what's my phone number?".
        The egress guard is unchanged: none of these is one of the values an
        outbound call may carry, except the wallets it already allowed.
        """
        if self._facts is None:
            return None
        try:
            stored = await self._facts.facts_for(
                viewer, ConversationLocation(viewer.person.platform, 0, direct=direct)
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
            email=stored.get(FactKind.EMAIL),
            phone=stored.get(FactKind.PHONE),
            home_address=stored.get(FactKind.HOME_ADDRESS),
            birth_date=stored.get(FactKind.BIRTH_DATE),
            eth_wallets=stored.values(FactKind.ETH_WALLET),
            btc_wallets=stored.values(FactKind.BTC_WALLET),
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
