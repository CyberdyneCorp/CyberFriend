"""The answer path for decision questions.

"O que decidimos sobre o deploy?" is answered from the decision rows, the way
"what did people ask me" is answered from the ask rows: an `AnswerService`
wrapping another one, in front of reasoning, so everything it does not claim
reaches reasoning unchanged.

Two things differ from obligations, both on purpose.

**No "nothing was decided".** An empty obligation list is a true answer: asks
are read off every candidate message, and "nothing outstanding" is what the
rows say. Decisions are sparser and harder to extract, so an empty search
more likely means a missed extraction than a question nobody settled. Below
the similarity floor, or with no rows at all, the question goes to ordinary
retrieval, which can still find the conversation -- and which has its own,
single "found nothing" reply, so this path adds no second one to read absence
from.

**One embedding.** The topic is embedded once, which is what lets a
Portuguese question find an English decision. No chat model is called: the
reply is rendered from the rows, dated and cited, in the asker's language.

Scope comes from `reasoning.scope.retrieval_viewer`, the asker intersected
with the audience, so a #leadership decision is never listed in #general.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta, tzinfo

import structlog

from chatmemory.app.asks.obligations import MessageUrl
from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.decisions.model import DecisionPolicy, DecisionRequest, ReportedDecision
from chatmemory.app.decisions.ports import DecisionSearch
from chatmemory.app.decisions.render import render_decisions
from chatmemory.app.reasoning import features
from chatmemory.app.reasoning.contract import RunOutcome, answered, run_of
from chatmemory.app.reasoning.scope import retrieval_viewer
from chatmemory.app.routing import DecisionQuestion, decision_question
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.ports.answers import Answer, AnswerService, Question
from chatmemory.ports.sources import EmbeddingClient

log = structlog.get_logger()


def _no_url(channel: ChannelRef, message_id: int) -> str:
    return f"{channel}/{message_id}"


class DecisionAnswerService:
    """Answers decision questions from records; delegates everything else.

    Implements `AnswerService`, like `ObligationAnswerService`, which it sits
    behind.
    """

    def __init__(
        self,
        decisions: DecisionSearch,
        embeddings: EmbeddingClient,
        fallback: AnswerService,
        *,
        tz: tzinfo,
        clock: Clock = utc_now,
        policy: DecisionPolicy | None = None,
        message_url: MessageUrl = _no_url,
    ) -> None:
        self._decisions = decisions
        self._embeddings = embeddings
        self._fallback = fallback
        self._tz = tz
        self._clock = clock
        self._policy = policy or DecisionPolicy()
        self._url = message_url

    async def answer(self, question: Question) -> Answer:
        return (await self.answer_run(question)).answer

    async def answer_run(self, question: Question) -> RunOutcome:
        asked = decision_question(question.text, self._clock(), self._tz)
        if asked is None:
            return await run_of(self._fallback, question)
        viewer = retrieval_viewer(question)
        found = await self._lookup(viewer, asked)
        log.info(
            "decisions.looked_up",
            viewer=str(viewer.person),
            topic=bool(asked.topic),
            span=asked.span.label if asked.span else None,
            found=len(found),
            # Stated rather than implied, as for obligations: the only call
            # this path makes is the topic's embedding.
            model_calls=asked.model_calls,
        )
        if not found:
            return await run_of(self._fallback, question)
        rendered = render_decisions(
            found,
            asked,
            tz=self._tz,
            policy=self._policy,
            message_url=self._url,
        )
        return answered(rendered, features.DECISIONS)

    async def _lookup(
        self, viewer: Viewer, asked: DecisionQuestion
    ) -> Sequence[ReportedDecision]:
        if not viewer.visible_channels:
            return []
        vector = None
        if asked.topic:
            try:
                (vector,) = await self._embeddings.embed([asked.topic])
            except Exception:  # noqa: BLE001 - retrieval answers, and reports its own failure
                log.warning("decisions.topic_embedding_failed", exc_info=True)
                return []
        return await self._decisions.search(viewer, self._request(asked), vector)

    def _request(self, asked: DecisionQuestion) -> DecisionRequest:
        policy = self._policy
        if asked.span is not None:
            since, until = asked.span.start, asked.span.end
        elif not asked.topic:
            # "What did we decide?" names nothing to rank by or bound with; a
            # recent window, which the reply's heading states.
            since, until = self._clock() - timedelta(days=policy.default_days), None
        else:
            since, until = None, None
        return DecisionRequest(
            topic=asked.topic,
            since=since,
            until=until,
            # From policy, never from the question: a weak extraction does not
            # become a decision because somebody asked broadly.
            min_confidence=policy.min_confidence,
            min_similarity=policy.min_similarity,
            limit=policy.limit,
        )
