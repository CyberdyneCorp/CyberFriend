"""Capture a message, extract the ask, answer the question -- against real SQL.

The unit tests prove the pieces and the wiring; this proves the path. A
message is persisted exactly as the live loop persists it, handed to the
extraction worker exactly as the live loop hands it over, and then the
question "what did people ask me today" is asked through the same answer
service the bot is given -- with a real `PostgresAskStore` underneath, because
the permission predicate is SQL and a fake written by the same hand as the
code cannot prove it.

The model is stubbed. What is being tested is the pipeline, not the
extractor's judgement, and a test that spends money on every run gets
disabled the first week it is inconvenient.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.asks.answering import ObligationAnswerService
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import AskCandidate, AskKind, ExtractedAsk
from chatmemory.app.asks.obligations import NOTHING_OUTSTANDING
from chatmemory.app.asks.resolution import ObservedDirectory
from chatmemory.app.asks.state import AskStateService
from chatmemory.app.asks.worker import ExtractionWorker
from chatmemory.app.ingest import IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.composition import build_obligations
from chatmemory.config import Settings
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from chatmemory.ports.answers import Answer, Question
from chatmemory.ports.store import Store
from tests.integration.conftest import DB_URL

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
GUILD = 4242

OPEN_CH = ChannelRef(PLATFORM, 100)
PRIVATE_CH = ChannelRef(PLATFORM, 300)

ALICE = PersonRef(PLATFORM, 1)
BOB = PersonRef(PLATFORM, 2)

NOW = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
TODAY = NOW.replace(hour=9, minute=0)
THREAD = 77


@pytest.fixture(autouse=True)
async def _requires_ask_schema(clean: AsyncEngine) -> None:
    async with clean.connect() as conn:
        present = await conn.execute(text("SELECT to_regclass('public.ask')"))
        if present.scalar() is None:
            pytest.skip("ask tables are missing; run `alembic upgrade head`")


def settings() -> Settings:
    return Settings(
        discord_token=SecretStr("x"),
        discord_guild_id=GUILD,
        database_url=SecretStr(DB_URL),
        llm_api_key=SecretStr("k"),
        indexed_channel_ids=frozenset({100, 300}),
    )


def message(
    message_id: int,
    author: PersonRef,
    content: str,
    channel: ChannelRef = OPEN_CH,
    at: datetime | None = None,
    mentions: frozenset[PersonRef] = frozenset(),
    thread_id: int | None = None,
    reply_to_id: int | None = None,
) -> Message:
    return Message(
        platform_message_id=message_id,
        channel=channel,
        author=author,
        content=content,
        created_at=at or TODAY,
        mentions=mentions,
        thread_id=thread_id,
        reply_to_id=reply_to_id,
        author_display="alice" if author == ALICE else "bob",
    )


class StubExtractor:
    """One request per message, so the pipeline is what varies, not the model."""

    def __init__(self, kind: AskKind = AskKind.REQUEST, confidence: float = 0.9) -> None:
        self.kind = kind
        self.confidence = confidence
        self.calls: list[AskCandidate] = []

    async def extract(self, candidate: AskCandidate) -> Sequence[ExtractedAsk]:
        self.calls.append(candidate)
        return [
            ExtractedAsk(
                kind=self.kind,
                text="review the migration",
                confidence=self.confidence,
            )
        ]


class NeverAnswers:
    """The retrieval path, which an obligation question must never reach."""

    def __init__(self) -> None:
        self.questions: list[str] = []

    async def answer(self, question: Question) -> Answer:
        self.questions.append(question.text)
        return Answer(text="retrieval was consulted", abstained=True)


class Pipeline:
    """The two halves, over one engine, wired the way the processes wire them."""

    def __init__(self, engine: AsyncEngine, extractor: StubExtractor) -> None:
        self.store: Store = PostgresStore(engine)
        self.asks = PostgresAskStore(engine)
        self.extractor = extractor
        self.ingest = IngestService(
            source=None,  # type: ignore[arg-type]
            store=self.store,
            windows=WindowBuilder(max_messages=10, max_tokens=512, gap=timedelta(minutes=15)),
            indexed_channels=frozenset({100, 300}),
        )
        directory = ObservedDirectory()
        self.worker = ExtractionWorker(
            ExtractionService(
                extractor=extractor, store=self.asks, directory=directory
            ),
            window_messages=1,
            directory=directory,
        )
        self.state = AskStateService(self.asks)
        self.retrieval = NeverAnswers()
        self.answers = ObligationAnswerService(
            build_obligations(settings(), engine), self.retrieval, clock=lambda: NOW
        )

    async def capture(self, *messages: Message) -> None:
        """Exactly what `live_loop` does: persist, then offer to extraction."""
        for item in messages:
            assert await self.ingest.capture(item)
            self.worker.submit(item)
        await self.worker.flush_all()

    async def ask(self, text_: str, viewer: Viewer, *audience: ChannelRef) -> Answer:
        return await self.answers.answer(
            Question(
                text=text_,
                asker=viewer,
                audience=Audience(
                    mode=DeliveryMode.PUBLIC_CHANNEL,
                    members=frozenset({viewer.person}),
                    readable_channels=frozenset(audience),
                ),
            )
        )


def viewer(person: PersonRef, *channels: ChannelRef) -> Viewer:
    return Viewer(person=person, visible_channels=frozenset(channels))


@pytest.fixture
def pipeline(clean: AsyncEngine) -> Pipeline:
    return Pipeline(clean, StubExtractor())


# --- the path -----------------------------------------------------------


async def test_a_captured_message_becomes_an_answerable_obligation(
    pipeline: Pipeline,
) -> None:
    await pipeline.capture(
        message(10, ALICE, "@bob can you review the migration?", mentions=frozenset({BOB}))
    )

    answer = await pipeline.ask("what did people ask me today", viewer(BOB, OPEN_CH), OPEN_CH)

    assert pipeline.retrieval.questions == [], "an obligation question hit retrieval"
    assert len(answer.citations) == 1
    citation = answer.citations[0]
    assert citation.message_id == 10
    assert citation.author_display == "alice"
    # The citation is the whole defence against an inferred obligation: a
    # reader has to be able to open the message the claim came from.
    assert citation.url == f"https://discord.com/channels/{GUILD}/100/10"
    assert "review the migration" in answer.text


async def test_nothing_outstanding_is_a_successful_answer(pipeline: Pipeline) -> None:
    answer = await pipeline.ask("what do I need to do today", viewer(BOB, OPEN_CH), OPEN_CH)
    assert answer.text == NOTHING_OUTSTANDING
    assert not answer.abstained
    assert pipeline.retrieval.questions == []


async def test_asks_in_unreadable_channels_are_not_returned_reported_or_counted(
    pipeline: Pipeline,
) -> None:
    """The permission predicate is SQL, and this is where that is proved."""
    await pipeline.capture(
        message(
            11,
            ALICE,
            "@bob can you review the migration?",
            channel=PRIVATE_CH,
            mentions=frozenset({BOB}),
        )
    )

    restricted = viewer(BOB, OPEN_CH)
    answer = await pipeline.ask("what did people ask me today", restricted, OPEN_CH)

    assert answer.text == NOTHING_OUTSTANDING
    assert answer.citations == ()
    from chatmemory.app.asks.model import ObligationRequest

    assert await pipeline.asks.count_outstanding(restricted, ObligationRequest()) == 0
    # And the row is really there, so the test is proving the filter rather
    # than an empty table.
    assert await pipeline.asks.count_outstanding(
        viewer(BOB, OPEN_CH, PRIVATE_CH), ObligationRequest()
    ) == 1


async def test_a_deleted_source_message_stops_the_ask_immediately(
    pipeline: Pipeline,
) -> None:
    await pipeline.capture(
        message(12, ALICE, "@bob can you review the migration?", mentions=frozenset({BOB}))
    )
    before = await pipeline.ask("what did people ask me today", viewer(BOB, OPEN_CH), OPEN_CH)
    assert len(before.citations) == 1

    await pipeline.ingest.handle_delete(12, NOW, OPEN_CH, TODAY)

    after = await pipeline.ask("what did people ask me today", viewer(BOB, OPEN_CH), OPEN_CH)
    assert after.text == NOTHING_OUTSTANDING


async def test_an_ask_the_addressee_answered_leaves_the_list(pipeline: Pipeline) -> None:
    """The state pass is what makes this true, and it calls no model."""
    await pipeline.capture(
        message(
            13,
            ALICE,
            "@bob can you review the migration?",
            mentions=frozenset({BOB}),
            thread_id=THREAD,
        )
    )
    still_open = await pipeline.ask(
        "what do I need to do", viewer(BOB, OPEN_CH), OPEN_CH
    )
    assert len(still_open.citations) == 1

    # Bob answers in the thread. Nothing about this is a judgement.
    await pipeline.capture(
        message(
            14,
            BOB,
            "done, merged it",
            at=TODAY + timedelta(minutes=5),
            thread_id=THREAD,
            reply_to_id=13,
        )
    )
    refreshed = await pipeline.state.refresh(NOW)
    assert refreshed.answered_by_reply == 1

    answered = await pipeline.ask("what do I need to do", viewer(BOB, OPEN_CH), OPEN_CH)
    assert answered.text == NOTHING_OUTSTANDING


async def test_reprocessing_the_same_message_does_not_duplicate_the_ask(
    pipeline: Pipeline,
) -> None:
    """Re-extraction happens -- a window is re-formed, a batch is retried --
    and an obligation that reappears as a second entry reads as two."""
    captured = message(
        15, ALICE, "@bob can you review the migration?", mentions=frozenset({BOB})
    )
    await pipeline.capture(captured)
    await pipeline.capture(replace(captured, content="@bob can you review the migration!"))

    answer = await pipeline.ask("what did people ask me today", viewer(BOB, OPEN_CH), OPEN_CH)
    assert len(answer.citations) == 1


async def test_a_sub_threshold_extraction_is_recorded_but_never_reported(
    clean: AsyncEngine,
) -> None:
    """Storing it is how the threshold gets tuned; reporting it is what the
    confidence gate exists to stop."""
    pipeline = Pipeline(clean, StubExtractor(confidence=0.2))
    await pipeline.capture(
        message(16, ALICE, "@bob can you review the migration?", mentions=frozenset({BOB}))
    )

    answer = await pipeline.ask("what did people ask me today", viewer(BOB, OPEN_CH), OPEN_CH)
    assert answer.text == NOTHING_OUTSTANDING

    async with clean.connect() as conn:
        stored = await conn.execute(text("SELECT count(*) FROM ask WHERE source_message_id = 16"))
        assert stored.scalar_one() == 1


async def test_a_commitment_counts_towards_what_i_need_to_do_only(
    clean: AsyncEngine,
) -> None:
    """A promise is owed *by* the speaker, so it is not something anybody
    asked of them -- mixing the two makes both answers unverifiable."""
    pipeline = Pipeline(clean, StubExtractor(kind=AskKind.COMMITMENT))
    await pipeline.capture(message(17, BOB, "i'll push the fix tonight"))

    mine = await pipeline.ask("what do I need to do", viewer(BOB, OPEN_CH), OPEN_CH)
    assert len(mine.citations) == 1

    asked = await pipeline.ask(
        "what did people ask me today", viewer(BOB, OPEN_CH), OPEN_CH
    )
    assert asked.text == NOTHING_OUTSTANDING


async def test_only_messages_with_a_plausible_addressee_are_paid_for(
    pipeline: Pipeline,
) -> None:
    """Extraction is a standing cost proportional to traffic, and this is the
    control that keeps it proportional to *relevant* traffic."""
    await pipeline.capture(
        message(18, ALICE, "deploy finished, no errors in the log"),
        message(19, ALICE, "can you take a look at the migration"),
    )
    assert [c.message.platform_message_id for c in pipeline.extractor.calls] == [19]


async def test_a_reaction_predating_the_ask_does_not_close_it(
    pipeline: Pipeline,
) -> None:
    """The acknowledgement has to come after the thing it acknowledges.

    People react to messages all the time. Without the ordering guard, a tick
    left for some earlier reason closes the ask the moment extraction creates
    one -- the obligation is answered before anybody has read it.
    """
    await pipeline.capture(
        message(
            21,
            ALICE,
            "@bob can you review the migration?",
            mentions=frozenset({BOB}),
            thread_id=THREAD,
        )
    )

    # Reacted an hour before the ask was recorded.
    await pipeline.asks.record_reaction(
        source_message_id=21, person=BOB, emoji="\u2705", at=TODAY - timedelta(hours=1)
    )
    await pipeline.state.refresh(NOW)

    outstanding = await pipeline.ask(
        "what do I need to do", viewer(BOB, OPEN_CH), OPEN_CH
    )
    assert outstanding.text != NOTHING_OUTSTANDING, "a stale reaction closed the ask"


async def test_a_reaction_after_the_ask_does_close_it(pipeline: Pipeline) -> None:
    """The guard must not break the case it exists to permit."""
    await pipeline.capture(
        message(
            22,
            ALICE,
            "@bob can you review the migration?",
            mentions=frozenset({BOB}),
            thread_id=THREAD,
        )
    )
    await pipeline.asks.record_reaction(
        source_message_id=22, person=BOB, emoji="\u2705", at=TODAY + timedelta(minutes=5)
    )
    refreshed = await pipeline.state.refresh(NOW)
    assert refreshed.answered_by_reaction == 1
