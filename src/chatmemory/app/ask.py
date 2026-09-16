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
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace

import structlog

from chatmemory.app.asks.corrections import CorrectionService
from chatmemory.app.asks.model import (
    CorrectionOutcome,
    CorrectionResolution,
    ReportedAsk,
)
from chatmemory.app.confirmation import (
    ConfirmationChannel,
    ConfirmationDesk,
    ConfirmationSurface,
    attending,
)
from chatmemory.app.conversation import Conversations
from chatmemory.app.disclosure import ScopedAnswer, WithheldEvidenceProbe, enforce_audience
from chatmemory.app.limits import RateLimiter
from chatmemory.app.routing import indexing_request
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
from chatmemory.ports.memory import ConversationLocation, MemoryPurge, Recollection

log = structlog.get_logger()

INDEXING_POINTER = (
    "I can't change which channels are indexed from a chat message. Someone with "
    "Manage Channels on the channel can use `/index` or `/unindex` there; Discord "
    "decides who that is, not what the message says."
)


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
        self, request: AskRequest, confirm: ConfirmationSurface | None = None
    ) -> AskOutcome:
        decision = self._limiter.check(request.asker)
        if not decision.allowed:
            return AskOutcome(None, True, decision.retry_after_seconds)

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
        memory, profile = await asyncio.gather(
            self._recall(scope, location), self._profile(request.asker)
        )
        question = Question(
            text=request.text,
            asker=viewer,
            audience=audience,
            memory=memory,
            asker_profile=profile,
        )
        # Concurrent, and not merely for speed. Run after the answer, the
        # probe would add its latency only for askers who have channels the
        # room does not -- which makes "this person can see more than you" a
        # property of how long the bot took to reply.
        # The channel is opened around the whole of answer production, and
        # closed the moment it ends: a confirmation cannot be collected for a
        # run that is already over, and the next asker gets their own.
        # `gather` copies the current context into each task it starts, so a
        # tool call made deep inside the answer finds this channel and no
        # other.
        with self._attending(request.asker, confirm):
            answer, withheld = await asyncio.gather(
                self._answers.answer(question),
                self._withheld_channels(viewer, audience, request.text),
            )
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

    async def forget(self, request: ForgetRequest) -> MemoryPurge | None:
        """Erase the requester's own conversation, here or everywhere.

        None when this deployment keeps no memory, so a surface can say so
        rather than report a deletion of nothing as a success.
        """
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
