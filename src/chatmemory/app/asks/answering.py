"""The answer path for obligation questions.

This is the join between the two halves of the feature: extraction records
asks as conversation arrives, and this decides that "what did people ask me
today" is one of the questions those records exist to answer.

It is an `AnswerService` wrapping another one, rather than a branch inside the
reasoning service, for two reasons. Obligation answers are produced with *no*
model call and no similarity search, so they must not pass through a path
built around retrieving evidence and writing prose over it. And every question
this does not claim has to reach the reasoning service unchanged, so a
deployment gains the obligation path without changing how anything else is
answered.

Scope comes from `reasoning.scope.retrieval_viewer`, not from the asker alone.
An ask is a summary of a conversation, and a summary travels further than the
quote it came from; answering "what did people ask me" in a public channel
from private asks would publish exactly that.
"""

from __future__ import annotations

import structlog

from chatmemory.app.asks.obligations import ObligationService
from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.reasoning import features
from chatmemory.app.reasoning.contract import RunOutcome, answered, run_of
from chatmemory.app.reasoning.scope import retrieval_viewer
from chatmemory.app.routing import ObligationIntent, ObligationQuestion, obligation_question
from chatmemory.ports.answers import Answer, AnswerService, Question

log = structlog.get_logger()


class ObligationAnswerService:
    """Answers obligation questions from records; delegates everything else.

    Implements `AnswerService`, so it substitutes for the reasoning service at
    the one place the bot is handed one.
    """

    def __init__(
        self,
        obligations: ObligationService,
        fallback: AnswerService,
        clock: Clock = utc_now,
    ) -> None:
        self._obligations = obligations
        self._fallback = fallback
        self._clock = clock

    async def answer(self, question: Question) -> Answer:
        return (await self.answer_run(question)).answer

    async def answer_run(self, question: Question) -> RunOutcome:
        asked = obligation_question(question.text)
        if asked is None:
            return await run_of(self._fallback, question)
        return answered(await self._from_records(question, asked), features.OBLIGATIONS)

    async def _from_records(self, question: Question, asked: ObligationQuestion) -> Answer:

        # The viewer for the whole answer: the asker intersected with the
        # audience that will receive it. Taken from the question rather than
        # assembled here, so obligations are scoped by exactly the rule
        # retrieval is scoped by -- one rule, one place it can be wrong.
        viewer = retrieval_viewer(question)
        since, until = asked.period.bounds(self._clock()) if asked.period else (None, None)

        log.info(
            "asks.answered_from_records",
            intent=str(asked.intent),
            viewer=str(viewer.person),
            # Stated rather than implied: this path runs no similarity search
            # and calls no model, which is the whole reason the rows exist.
            model_calls=asked.model_calls,
            since=since.isoformat() if since else None,
        )
        if asked.intent is ObligationIntent.ASKED_OF_ME:
            return await self._obligations.asked_of_me(viewer, since=since, until=until)
        # The period is deliberately dropped here. "What do I need to do
        # today" is a question about what is outstanding *now*, not about what
        # was asked today: a request made last Tuesday and still open is the
        # first thing that answer should contain.
        return await self._obligations.what_i_need_to_do(viewer)
