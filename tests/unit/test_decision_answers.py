"""The decision answer path over fakes: what it asks for, and what it says.

The store's half -- the ACL, the evidence predicate, the ranking -- is in
tests/integration/test_decisions_search.py. Here the fakes record every viewer,
request and embedding, so what is asserted is what the path asks for and how
it renders what it gets back.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from chatmemory.app.asks.obligations import discord_message_url
from chatmemory.app.decisions.answering import DecisionAnswerService
from chatmemory.app.decisions.model import (
    DecisionPolicy,
    DecisionRequest,
    ReportedDecision,
    search_terms,
)
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer, Question

TZ = ZoneInfo("America/Sao_Paulo")
#: Tuesday 1 September 2026, 12:00 UTC.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
GUILD = 555

GENERAL = ChannelRef("discord", 100)
LEADERSHIP = ChannelRef("discord", 300)
ASKER = PersonRef("discord", 7)

FALLBACK = Answer(text="from retrieval", consulted_channels=frozenset())


class FakeDecisions:
    def __init__(self, found: Sequence[ReportedDecision] = ()) -> None:
        self.found = list(found)
        self.calls: list[tuple[Viewer, DecisionRequest, Sequence[float] | None]] = []

    async def search(
        self, viewer: Viewer, request: DecisionRequest, query_embedding: Sequence[float] | None
    ) -> Sequence[ReportedDecision]:
        self.calls.append((viewer, request, query_embedding))
        return self.found


class FakeEmbeddings:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.texts: list[str] = []

    @property
    def dimensions(self) -> int:
        return 3

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        if self.fail:
            raise RuntimeError("embedding endpoint unavailable")
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeFallback:
    def __init__(self) -> None:
        self.questions: list[Question] = []

    async def answer(self, question: Question) -> Answer:
        self.questions.append(question)
        return FALLBACK


def question(
    text: str,
    visible: frozenset[ChannelRef] = frozenset({GENERAL, LEADERSHIP}),
    readable: frozenset[ChannelRef] = frozenset({GENERAL, LEADERSHIP}),
) -> Question:
    return Question(
        text=text,
        asker=Viewer(ASKER, visible),
        audience=Audience(
            mode=DeliveryMode.DIRECT_MESSAGE, members=frozenset({ASKER}), readable_channels=readable
        ),
    )


def reported(message_id: int, summary: str, at: datetime) -> ReportedDecision:
    return ReportedDecision(
        source_message_id=message_id,
        channel=GENERAL,
        summary=summary,
        topic="deploy",
        decided_at=at,
        author_display="Leo",
        source_excerpt=f"original words {message_id}",
    )


LATER = reported(2, "O deploy volta para quinta", datetime(2026, 8, 28, 1, 30, tzinfo=UTC))
EARLIER = reported(1, "O deploy passa a ser na sexta", datetime(2026, 8, 20, 15, tzinfo=UTC))


def service(
    decisions: FakeDecisions,
    embeddings: FakeEmbeddings | None = None,
    fallback: FakeFallback | None = None,
) -> tuple[DecisionAnswerService, FakeEmbeddings, FakeFallback]:
    embeddings = embeddings or FakeEmbeddings()
    fallback = fallback or FakeFallback()
    built = DecisionAnswerService(
        decisions,
        embeddings,
        fallback,
        tz=TZ,
        clock=lambda: NOW,
        policy=DecisionPolicy(min_confidence=0.7, min_similarity=0.4, limit=5),
        message_url=discord_message_url(GUILD),
    )
    return built, embeddings, fallback


async def test_a_portuguese_question_is_answered_dated_and_cited_in_portuguese() -> None:
    decisions = FakeDecisions([LATER, EARLIER])
    answers, embeddings, fallback = service(decisions)

    answer = await answers.answer(question("o que decidimos sobre o deploy?"))

    assert answer.text.splitlines() == [
        "Decisões sobre o deploy:",
        # Local dates: 01:30 UTC on the 28th is still the 27th in Sao Paulo.
        "1. 27/08/2026 — Leo: O deploy volta para quinta [1]",
        "2. 20/08/2026 — Leo: O deploy passa a ser na sexta [2]",
    ]
    assert [c.url for c in answer.citations] == [
        f"https://discord.com/channels/{GUILD}/{GENERAL.platform_channel_id}/2",
        f"https://discord.com/channels/{GUILD}/{GENERAL.platform_channel_id}/1",
    ]
    # The citation quotes the original message, not the summary.
    assert answer.citations[0].excerpt == "original words 2"
    assert answer.consulted_channels == frozenset({GENERAL})
    assert embeddings.texts == ["o deploy"], "one embedding, of the topic"
    assert fallback.questions == []


async def test_an_english_question_is_answered_in_english() -> None:
    answers, _, _ = service(FakeDecisions([LATER]))

    answer = await answers.answer(question("what did we decide about the deploy last week?"))

    assert answer.text.splitlines() == [
        "Decisions about the deploy from 24 Aug to 30 Aug:",
        "1. 27 Aug 2026 — Leo: O deploy volta para quinta [1]",
    ]


async def test_the_request_carries_the_span_and_the_policy_thresholds() -> None:
    decisions = FakeDecisions([LATER])
    answers, _, _ = service(decisions)

    await answers.answer(question("o que decidimos sobre o deploy semana passada?"))

    ((_, request, vector),) = decisions.calls
    assert request == DecisionRequest(
        topic="o deploy",
        since=datetime(2026, 8, 24, 3, tzinfo=UTC),
        until=datetime(2026, 8, 31, 3, tzinfo=UTC),
        min_confidence=0.7,
        min_similarity=0.4,
        limit=5,
    )
    assert vector == [1.0, 0.0, 0.0]


async def test_a_bare_question_lists_the_last_thirty_days_and_says_so() -> None:
    decisions = FakeDecisions([LATER])
    answers, embeddings, _ = service(decisions)

    answer = await answers.answer(question("o que foi decidido?"))

    ((_, request, vector),) = decisions.calls
    assert request.since == NOW - timedelta(days=30) and request.until is None
    assert vector is None and embeddings.texts == [], "nothing to embed"
    assert answer.text.splitlines()[0] == "Decisões dos últimos 30 dias:"


async def test_a_period_without_a_topic_is_named_in_the_heading() -> None:
    answers, _, _ = service(FakeDecisions([LATER]))

    answer = await answers.answer(question("what did we decide last week?"))

    assert answer.text.splitlines()[0] == "Decisions from 24 Aug to 30 Aug:"


async def test_the_viewer_is_the_asker_intersected_with_the_audience() -> None:
    decisions = FakeDecisions([LATER])
    answers, _, _ = service(decisions)

    await answers.answer(
        question("o que decidimos sobre o deploy?", readable=frozenset({GENERAL}))
    )

    ((viewer, _, _),) = decisions.calls
    assert viewer.visible_channels == frozenset({GENERAL})


async def test_nothing_found_falls_through_to_retrieval_never_to_nothing_decided() -> None:
    answers, _, fallback = service(FakeDecisions([]))
    asked = question("o que decidimos sobre o deploy?")

    assert await answers.answer(asked) is FALLBACK
    assert fallback.questions == [asked]


async def test_a_failed_embedding_falls_through_without_searching() -> None:
    decisions = FakeDecisions([LATER])
    answers, _, fallback = service(decisions, FakeEmbeddings(fail=True))

    assert await answers.answer(question("what did we decide about the deploy?")) is FALLBACK
    assert decisions.calls == []
    assert len(fallback.questions) == 1


async def test_no_readable_channel_searches_nothing() -> None:
    decisions = FakeDecisions([LATER])
    answers, embeddings, _ = service(decisions)

    answer = await answers.answer(
        question("what did we decide about the deploy?", readable=frozenset())
    )

    assert answer is FALLBACK
    assert decisions.calls == [] and embeddings.texts == []


async def test_every_other_question_is_passed_through_untouched() -> None:
    decisions = FakeDecisions([LATER])
    answers, embeddings, fallback = service(decisions)
    asked = question("what did you decide about the deploy?")

    assert await answers.answer(asked) is FALLBACK
    assert fallback.questions == [asked]
    assert decisions.calls == [] and embeddings.texts == []


def test_filler_words_are_not_searched_for() -> None:
    assert search_terms("o deploy") == "deploy"
    assert search_terms("the Deploy of the API") == "deploy api"
    assert search_terms("a Migração do Banco") == "migração banco"
