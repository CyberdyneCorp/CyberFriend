"""Decision rows against a real database: the write, the prune, and every way out.

What a fake cannot prove is what Postgres does with the row once it exists:
that the source's cascade takes it, that retention and opt-out reach it through
the evidence array (which no foreign key covers), and that re-extraction after
an edit withdraws it rather than leaving the old conclusion standing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.adapters.store.decisions_postgres import PostgresDecisionStore
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import AskCandidate, Extraction
from chatmemory.app.asks.resolution import StaticDirectory
from chatmemory.app.decisions.model import Decision, ExtractedDecision, decision_key
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

pytestmark = pytest.mark.asyncio

CH = 810
CHANNEL = ChannelRef("discord", CH)
LEO = PersonRef("discord", 8101)
BEA = PersonRef("discord", 8102)

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)
DIMS = 1536

PROPOSAL = "que tal deploy na sexta?"
CONCLUSION = "fechou, deploy na sexta então"


@pytest.fixture(autouse=True)
async def _requires_decision_schema(clean: AsyncEngine) -> None:
    async with clean.connect() as conn:
        present = await conn.execute(text("SELECT to_regclass('public.decision')"))
        if present.scalar() is None:
            pytest.skip("decision table is missing; run `alembic upgrade head`")


class FixedEmbeddings:
    """One unit vector per text; can be told to fail, as an endpoint can."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.texts: list[str] = []

    @property
    def dimensions(self) -> int:
        return DIMS

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        if self.fail:
            raise RuntimeError("embedding endpoint unavailable")
        return [[1.0] + [0.0] * (DIMS - 1) for _ in texts]


class ScriptedExtractor:
    """Reports the decisions scripted for a message's current content."""

    def __init__(self, by_content: dict[str, Sequence[ExtractedDecision]]) -> None:
        self.by_content = by_content

    async def extract(self, candidate: AskCandidate) -> Extraction:
        return Extraction(decisions=tuple(self.by_content.get(candidate.message.content, ())))


def msg(mid: int, author: PersonRef, content: str, at: datetime) -> Message:
    return Message(
        platform_message_id=mid, channel=CHANNEL, author=author, content=content, created_at=at
    )


def decision(source: Message, topic: str = "deploy", evidence: Sequence[int] = ()) -> Decision:
    return Decision(
        key=decision_key(source.platform_message_id, topic),
        source_message_id=source.platform_message_id,
        channel=source.channel,
        author=source.author,
        summary=f"O {topic} passa a ser na sexta",
        topic=topic,
        confidence=0.9,
        decided_at=source.created_at,
        evidence_message_ids=(source.platform_message_id, *evidence),
    )


async def seed(engine: AsyncEngine, *messages: Message) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO channel (id, platform, name, is_indexed) "
                "VALUES (:id, 'discord', 'decisions', TRUE) ON CONFLICT DO NOTHING"
            ),
            {"id": CH},
        )
    await PostgresStore(engine).upsert_messages(list(messages))


async def rows(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT decision_key, source_message_id, evidence_message_ids, summary, "
                "embedding IS NOT NULL AS embedded FROM decision ORDER BY decision_key"
            )
        )
        return [dict(r) for r in found.mappings()]


async def test_recording_twice_is_one_row_and_a_new_topic_replaces_the_old(
    clean: AsyncEngine,
) -> None:
    source = msg(2, LEO, CONCLUSION, NOW)
    await seed(clean, source)
    store = PostgresDecisionStore(clean, FixedEmbeddings())

    assert await store.record_decisions(2, [decision(source)]) == 1
    await store.record_decisions(2, [decision(source)])
    assert [r["decision_key"] for r in await rows(clean)] == ["2:deploy"]

    await store.record_decisions(2, [decision(source, topic="release")])
    assert [r["decision_key"] for r in await rows(clean)] == ["2:release"]


async def test_the_embedding_is_written_and_a_failed_one_leaves_null(
    clean: AsyncEngine,
) -> None:
    first, second = msg(2, LEO, CONCLUSION, NOW), msg(3, BEA, "combinado, retro quinzenal", NOW)
    await seed(clean, first, second)
    embeddings = FixedEmbeddings()

    await PostgresDecisionStore(clean, embeddings).record_decisions(2, [decision(first)])
    await PostgresDecisionStore(clean, FixedEmbeddings(fail=True)).record_decisions(
        3, [decision(second, topic="retro")]
    )

    embedded = {r["source_message_id"]: r["embedded"] for r in await rows(clean)}
    assert embedded == {2: True, 3: False}
    assert embeddings.texts == ["deploy: O deploy passa a ser na sexta"]


async def test_a_failed_embedding_does_not_erase_a_stored_one(clean: AsyncEngine) -> None:
    source = msg(2, LEO, CONCLUSION, NOW)
    await seed(clean, source)
    await PostgresDecisionStore(clean, FixedEmbeddings()).record_decisions(2, [decision(source)])

    await PostgresDecisionStore(clean, FixedEmbeddings(fail=True)).record_decisions(
        2, [decision(source)]
    )

    assert [r["embedded"] for r in await rows(clean)] == [True]


async def test_the_search_vector_is_simple_so_portuguese_words_survive(
    clean: AsyncEngine,
) -> None:
    source = msg(2, LEO, CONCLUSION, NOW)
    await seed(clean, source)
    await PostgresDecisionStore(clean, FixedEmbeddings()).record_decisions(2, [decision(source)])

    async with clean.connect() as conn:
        hit = await conn.execute(
            text(
                "SELECT count(*) FROM decision "
                "WHERE search_tsv @@ to_tsquery('simple', 'sexta & deploy')"
            )
        )
        assert hit.scalar_one() == 1


async def test_an_edit_that_takes_the_decision_back_withdraws_it(clean: AsyncEngine) -> None:
    """Through the extraction service, as the worker runs it after an edit."""
    proposal = msg(1, BEA, PROPOSAL, NOW - timedelta(minutes=1))
    source = msg(2, LEO, CONCLUSION, NOW)
    await seed(clean, proposal, source)
    extractor = ScriptedExtractor(
        {CONCLUSION: [ExtractedDecision("O deploy passa a ser na sexta", "deploy", 0.9)]}
    )
    service = ExtractionService(
        extractor=extractor,
        store=PostgresAskStore(clean),
        directory=StaticDirectory({}),
        decisions=PostgresDecisionStore(clean, FixedEmbeddings()),
    )

    await service.extract_window([proposal, source])
    stored = await rows(clean)
    assert [r["decision_key"] for r in stored] == ["2:deploy"]
    assert stored[0]["evidence_message_ids"] == [2, 1]

    edited = msg(2, LEO, "fechou? não, deploy na sexta ainda em aberto", NOW)
    await service.extract_window([proposal, edited])
    assert await rows(clean) == []


async def test_a_message_that_stops_being_a_candidate_loses_its_decision(
    clean: AsyncEngine,
) -> None:
    source = msg(2, LEO, CONCLUSION, NOW)
    await seed(clean, source)
    store = PostgresDecisionStore(clean, FixedEmbeddings())
    await store.record_decisions(2, [decision(source)])

    assert await store.withdraw([2, 99]) == 1
    assert await rows(clean) == []


async def test_deleting_the_source_message_cascades_to_its_decision(clean: AsyncEngine) -> None:
    source = msg(2, LEO, CONCLUSION, NOW)
    await seed(clean, source)
    await PostgresDecisionStore(clean, FixedEmbeddings()).record_decisions(2, [decision(source)])

    async with clean.begin() as conn:
        await conn.execute(text("DELETE FROM message WHERE id = 2"))

    assert await rows(clean) == []


async def test_retention_takes_old_decisions_and_those_resting_on_old_evidence(
    clean: AsyncEngine,
) -> None:
    ancient = msg(1, LEO, "fechou, vamos com Mongo", CUTOFF - timedelta(days=3))
    old_proposal = msg(2, BEA, PROPOSAL, CUTOFF - timedelta(minutes=5))
    straddling = msg(3, LEO, CONCLUSION, CUTOFF + timedelta(minutes=5))
    recent = msg(4, LEO, "combinado, retro quinzenal", NOW)
    await seed(clean, ancient, old_proposal, straddling, recent)
    store = PostgresDecisionStore(clean, FixedEmbeddings())
    await store.record_decisions(1, [decision(ancient, topic="banco")])
    await store.record_decisions(3, [decision(straddling, evidence=[2])])
    await store.record_decisions(4, [decision(recent, topic="retro")])

    purged = await PostgresRetentionStore(clean).purge_corpus_before(CUTOFF)

    # The straddling decision is inside the window, but its summary may carry
    # the words of a proposal that is not.
    assert purged.decisions == 2
    assert [r["decision_key"] for r in await rows(clean)] == ["4:retro"]


async def test_opting_out_removes_decisions_they_stated_or_proposed(clean: AsyncEngine) -> None:
    proposal = msg(1, BEA, PROPOSAL, NOW - timedelta(minutes=2))
    agreed = msg(2, LEO, CONCLUSION, NOW - timedelta(minutes=1))
    stated = msg(3, BEA, "combinado, retro quinzenal", NOW)
    unrelated = msg(4, LEO, "fechou, vamos com Kafka", NOW)
    await seed(clean, proposal, agreed, stated, unrelated)
    store = PostgresDecisionStore(clean, FixedEmbeddings())
    await store.record_decisions(2, [decision(agreed, evidence=[1])])
    await store.record_decisions(3, [decision(stated, topic="retro")])
    await store.record_decisions(4, [decision(unrelated, topic="kafka")])

    purged = await PostgresRetentionStore(clean).purge_person(BEA)

    # Leo's decision goes too: Bea wrote the proposal it settles.
    assert purged.decisions == 2
    assert [r["decision_key"] for r in await rows(clean)] == ["4:kafka"]
