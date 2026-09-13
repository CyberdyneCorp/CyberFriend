"""The question-answering use case, independent of Discord.

Everything platform-specific stays in the adapter. This orchestrates:
resolve who is asking, resolve who will see the answer, produce it bounded by
that audience, and re-check every citation before it leaves.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from chatmemory.app.conversation import ConversationStore
from chatmemory.app.disclosure import ScopedAnswer, enforce_audience
from chatmemory.app.limits import RateLimiter
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.acl import AclResolver, AudienceResolver
from chatmemory.ports.answers import Answer, AnswerService, Question

log = structlog.get_logger()


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
        conversations: ConversationStore,
    ) -> None:
        self._acl = acl
        self._audiences = audiences
        self._answers = answers
        self._limiter = limiter
        self._conversations = conversations

    async def ask(self, request: AskRequest) -> AskOutcome:
        decision = self._limiter.check(request.asker)
        if not decision.allowed:
            return AskOutcome(None, True, decision.retry_after_seconds)

        # Scope is always per-asker, even mid-conversation: a follow-up from a
        # different person must not inherit the previous asker's access.
        viewer = await self._acl.resolve_viewer(request.asker)

        if request.destination is None:
            audience = await self._audiences.resolve_private(request.asker)
        else:
            audience = await self._audiences.resolve_for_channel(request.destination)

        question = Question(
            text=request.text,
            asker=viewer,
            audience=audience,
            history=self._conversations.history(request.location_id),
        )
        answer = await self._answers.answer(question)
        scoped = enforce_audience(answer, audience, viewer)

        self._conversations.record(request.location_id, request.text)

        log.info(
            "ask.answered",
            asker=str(request.asker),
            mode=audience.mode,
            citations=len(scoped.answer.citations),
            withheld=len(scoped.withheld_from_audience),
            abstained=scoped.answer.abstained,
        )
        return AskOutcome(scoped)


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
