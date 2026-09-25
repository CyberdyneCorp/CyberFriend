"""The decision read against a real database: who may see a decision, and when.

What the SQL audit reads off the statement, proven on rows: a decision in a
channel the viewer cannot read is never returned, nor is one whose source or
any evidence message is deleted or outside the viewer's channels. Also the
thresholds -- confidence, period, similarity floor -- and the order.

The store writes these rows the way extraction does; the tombstones are
applied directly, without the withdrawal a deletion also triggers, so what is
tested is the read predicate on its own: the guard for a deletion that lands
between the extraction and the question.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.decisions_postgres import PostgresDecisionStore
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.decisions.model import Decision, DecisionRequest, decision_key
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message

pytestmark = pytest.mark.asyncio

GENERAL = ChannelRef("discord", 820)
LEADERSHIP = ChannelRef("discord", 821)
LEO = PersonRef("discord", 8201)
BEA = PersonRef("discord", 8202)
DIMS = 1536

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)

#: One axis per subject, so a similarity is exactly 1 or 0.
AXES = ("deploy", "café", "postgres")


@pytest.fixture(autouse=True)
async def _requires_decision_schema(clean: AsyncEngine) -> None:
    async with clean.connect() as conn:
        present = await conn.execute(text("SELECT to_regclass('public.decision')"))
        if present.scalar() is None:
            pytest.skip("decision table is missing; run `alembic upgrade head`")


class AxisEmbeddings:
    """A unit vector on the axis of the first subject the text names."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    @property
    def dimensions(self) -> int:
        return DIMS

    def vector(self, value: str) -> list[float]:
        values = [0.0] * DIMS
        axis = next((i for i, word in enumerate(AXES) if word in value.lower()), len(AXES))
        values[axis] = 1.0
        return values

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedding endpoint unavailable")
        return [self.vector(t) for t in texts]


def viewer(*channels: ChannelRef) -> Viewer:
    return Viewer(person=BEA, visible_channels=frozenset(channels))


def request(topic: str = "deploy", **overrides: object) -> DecisionRequest:
    values: dict[str, object] = {
        "topic": topic,
        "since": None,
        "until": None,
        "min_confidence": 0.7,
        "min_similarity": 0.4,
        "limit": 5,
    }
    values.update(overrides)
    return DecisionRequest(**values)  # type: ignore[arg-type]


def msg(mid: int, channel: ChannelRef, content: str, at: datetime) -> Message:
    return Message(
        platform_message_id=mid,
        channel=channel,
        author=LEO,
        content=content,
        created_at=at,
        author_display="Leo",
    )


def decision(
    source: Message,
    summary: str,
    topic: str = "deploy",
    evidence: Sequence[int] = (),
    confidence: float = 0.9,
) -> Decision:
    return Decision(
        key=decision_key(source.platform_message_id, topic),
        source_message_id=source.platform_message_id,
        channel=source.channel,
        author=source.author,
        summary=summary,
        topic=topic,
        confidence=confidence,
        decided_at=source.created_at,
        evidence_message_ids=(source.platform_message_id, *evidence),
    )


async def seed(engine: AsyncEngine, *messages: Message) -> None:
    async with engine.begin() as conn:
        for channel, name in ((GENERAL, "general"), (LEADERSHIP, "leadership")):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT DO NOTHING"
                ),
                {"id": channel.platform_channel_id, "n": name},
            )
    await PostgresStore(engine).upsert_messages(list(messages))


async def record(store: PostgresDecisionStore, *decisions: Decision) -> None:
    for item in decisions:
        await store.record_decisions(item.source_message_id, [item])


async def search(
    store: PostgresDecisionStore, who: Viewer, asked: DecisionRequest
) -> list[str]:
    vector = AxisEmbeddings().vector(asked.topic) if asked.topic else None
    return [d.summary for d in await store.search(who, asked, vector)]


async def tombstone(engine: AsyncEngine, message_id: int) -> None:
    await PostgresStore(engine).tombstone_message(message_id, NOW)


async def test_a_decision_is_visible_only_from_its_own_channel(clean: AsyncEngine) -> None:
    public = msg(1, GENERAL, "fechou, deploy na sexta", NOW - timedelta(days=2))
    private = msg(2, LEADERSHIP, "fechado: deploy congelado", NOW - timedelta(days=1))
    await seed(clean, public, private)
    store = PostgresDecisionStore(clean, AxisEmbeddings())
    await record(
        store,
        decision(public, "O deploy passa a ser na sexta"),
        decision(private, "O deploy fica congelado"),
    )

    assert await search(store, viewer(GENERAL), request()) == ["O deploy passa a ser na sexta"]
    assert await search(store, viewer(GENERAL, LEADERSHIP), request()) == [
        "O deploy fica congelado",
        "O deploy passa a ser na sexta",
    ]
    assert await search(store, viewer(), request()) == []


async def test_evidence_in_an_unreadable_channel_withholds_the_decision(
    clean: AsyncEngine,
) -> None:
    """The summary may restate the private message; so it is not shown."""
    proposal = msg(1, LEADERSHIP, "que tal deploy na sexta?", NOW - timedelta(hours=2))
    conclusion = msg(2, GENERAL, "fechou, deploy na sexta", NOW - timedelta(hours=1))
    await seed(clean, proposal, conclusion)
    store = PostgresDecisionStore(clean, AxisEmbeddings())
    await record(store, decision(conclusion, "O deploy passa a ser na sexta", evidence=[1]))

    assert await search(store, viewer(GENERAL), request()) == []
    assert await search(store, viewer(GENERAL, LEADERSHIP), request()) == [
        "O deploy passa a ser na sexta"
    ]


async def test_a_deleted_source_or_evidence_message_withholds_the_decision(
    clean: AsyncEngine,
) -> None:
    proposal = msg(1, GENERAL, "que tal deploy na sexta?", NOW - timedelta(hours=3))
    conclusion = msg(2, GENERAL, "fechou, deploy na sexta", NOW - timedelta(hours=2))
    other = msg(3, GENERAL, "combinado, deploy com feature flag", NOW - timedelta(hours=1))
    await seed(clean, proposal, conclusion, other)
    store = PostgresDecisionStore(clean, AxisEmbeddings())
    await record(
        store,
        decision(conclusion, "O deploy passa a ser na sexta", evidence=[1]),
        decision(other, "O deploy sai com feature flag"),
    )
    assert len(await search(store, viewer(GENERAL), request())) == 2

    await tombstone(clean, 1)  # the proposal, evidence of the first decision
    assert await search(store, viewer(GENERAL), request()) == ["O deploy sai com feature flag"]

    await tombstone(clean, 3)  # the second decision's own source
    assert await search(store, viewer(GENERAL), request()) == []


async def test_evidence_the_corpus_never_stored_withholds_the_decision(
    clean: AsyncEngine,
) -> None:
    """A purged evidence row reads as missing, not as fine."""
    conclusion = msg(2, GENERAL, "fechou, deploy na sexta", NOW)
    await seed(clean, conclusion)
    store = PostgresDecisionStore(clean, AxisEmbeddings())
    await record(store, decision(conclusion, "O deploy passa a ser na sexta", evidence=[999]))

    assert await search(store, viewer(GENERAL), request()) == []


async def test_confidence_period_and_similarity_bound_what_is_listed(
    clean: AsyncEngine,
) -> None:
    weak = msg(1, GENERAL, "bora de deploy na sexta", NOW - timedelta(days=10))
    old = msg(2, GENERAL, "fechou, deploy na quinta", NOW - timedelta(days=40))
    recent = msg(3, GENERAL, "fechou, deploy na sexta", NOW - timedelta(days=1))
    coffee = msg(4, GENERAL, "combinado, café novo", NOW - timedelta(days=2))
    await seed(clean, weak, old, recent, coffee)
    store = PostgresDecisionStore(clean, AxisEmbeddings())
    await record(
        store,
        decision(weak, "Talvez deploy na sexta", confidence=0.5),
        decision(old, "O deploy passa a ser na quinta"),
        decision(recent, "O deploy passa a ser na sexta"),
        decision(coffee, "Trocar a máquina de café", topic="café"),
    )
    everyone = viewer(GENERAL)

    assert await search(store, everyone, request()) == [
        "O deploy passa a ser na sexta",
        "O deploy passa a ser na quinta",
    ]
    since = request(since=NOW - timedelta(days=30), until=NOW)
    assert await search(store, everyone, since) == ["O deploy passa a ser na sexta"]
    # Nothing clears the floor: the caller falls through to retrieval.
    assert await search(store, everyone, request(topic="postgres")) == []
    # No topic lists the period's decisions, whatever they are about.
    assert await search(store, everyone, request(topic="", since=NOW - timedelta(days=5))) == [
        "O deploy passa a ser na sexta",
        "Trocar a máquina de café",
    ]


async def test_an_unembedded_decision_is_found_by_its_words_only(clean: AsyncEngine) -> None:
    conclusion = msg(1, GENERAL, "fechou, postgres no lugar do mysql", NOW)
    await seed(clean, conclusion)
    store = PostgresDecisionStore(clean, AxisEmbeddings(fail=True))
    await record(store, decision(conclusion, "Trocar o MySQL por Postgres", topic="banco"))

    assert await search(store, viewer(GENERAL), request(topic="o postgres")) == [
        "Trocar o MySQL por Postgres"
    ]
    assert await search(store, viewer(GENERAL), request(topic="deploy")) == []


async def test_the_best_matches_are_listed_newest_first(clean: AsyncEngine) -> None:
    older = msg(1, GENERAL, "fechou, deploy na sexta", NOW - timedelta(days=3))
    newer = msg(2, GENERAL, "fechou, deploy na quinta", NOW - timedelta(days=1))
    coffee = msg(3, GENERAL, "combinado, café novo", NOW)
    await seed(clean, older, newer, coffee)
    store = PostgresDecisionStore(clean, AxisEmbeddings())
    await record(
        store,
        decision(older, "O deploy passa a ser na sexta"),
        decision(newer, "O deploy volta para quinta"),
        decision(coffee, "Trocar a máquina de café", topic="café"),
    )

    found = await store.search(
        viewer(GENERAL), request(limit=1), AxisEmbeddings().vector("deploy")
    )
    assert [d.summary for d in found] == ["O deploy volta para quinta"]

    found = await store.search(viewer(GENERAL), request(), AxisEmbeddings().vector("deploy"))
    assert [d.summary for d in found] == [
        "O deploy volta para quinta",
        "O deploy passa a ser na sexta",
    ]
    (first, _) = found
    assert first.author_display == "Leo"
    assert first.source_excerpt == "fechou, deploy na quinta"
    assert first.channel == GENERAL
