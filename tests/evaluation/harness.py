"""Seeding and sweeping, against the objects the bot actually runs.

Nothing here reimplements retrieval. The corpus goes in through
`IngestService` and `WindowBuilder`, gets its vectors from `EmbeddingWorker`
and the real embedding client, and every measured search is
`HybridSearch.search` -- the same instance type `composition.build_answer_stack`
hands to `CorpusRetrieval`, called with the same required viewer. A golden set
that scored its own copy of the ranking would measure the copy.

The only thing the harness computes for itself is the mapping from a returned
window back to message ids, and it takes those from the search result rather
than from a second query, so a hit that arrives without provenance is visible
as such instead of being quietly repaired.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy import text
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.discord.acl import DiscordAclResolver, static_guild
from chatmemory.adapters.llm.embeddings import OpenAICompatibleEmbeddings
from chatmemory.adapters.store import sql
from chatmemory.adapters.store.postgres import HybridSearch, PostgresStore
from chatmemory.app.evaluation.scoring import (
    DEFAULT_K,
    AclBreach,
    GoldenReport,
    Provenance,
    QuestionScore,
    RankedResult,
    acl_breaches,
    score_question,
)
from chatmemory.app.ingest import EmbeddingWorker, IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from chatmemory.domain.search import SearchHit, SearchQuery
from chatmemory.entrypoints.ingest import rebuild_pending_windows
from chatmemory.ports.store import Store
from tests.evaluation import corpus
from tests.evaluation.questions import QUESTIONS, GoldenQuestion, Leg
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

log = structlog.get_logger()

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
BASE_URL = "https://api.openai.com/v1"

SEARCH_LIMIT = 20
"""What a search asks for. The scoring depth is `DEFAULT_K`, which is smaller:
the extra results are not scored for relevance but *are* checked for
permission, because a leak at rank 17 is the same disclosure as one at rank 1.
"""


class UnusedSource:
    """`IngestService` needs a `ChatSource`; this corpus never reads history.

    Present rather than absent so the service under test is the real one. Both
    methods raise, so a future refactor that starts pulling history here fails
    instead of silently seeding something the judgements do not describe.
    """

    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]:
        raise AssertionError("the golden corpus is seeded by capture, not backfill")

    def stream(self) -> object:
        raise AssertionError("the golden corpus is seeded by capture, not by a stream")


def guild() -> FakeGuild:
    """The permission source the real resolver reads."""
    return FakeGuild(
        id=corpus.GUILD,
        members=[
            FakeMember(user_id, roles) for user_id, roles in corpus.ROLES.items()
        ],
        text_channels=[
            FakeChannel(corpus.GENERAL, public=True),
            FakeChannel(corpus.ENGINEERING, allowed_roles=frozenset({"eng"})),
            FakeChannel(corpus.LEADERSHIP, allowed_roles=frozenset({"lead"})),
            FakeChannel(corpus.DESIGN, allowed_roles=frozenset({"design"})),
        ],
    )


async def resolve_viewers() -> dict[int, Viewer]:
    """Every viewer, through `DiscordAclResolver` rather than by hand.

    A viewer built in the harness would only prove the SQL filters by whatever
    it was handed. Resolving here means the channel sets under test are the
    ones the bot computes for those people.
    """
    resolver = DiscordAclResolver(static_guild(guild()), corpus.INDEXED)
    return {
        user_id: await resolver.resolve_viewer(PersonRef(corpus.PLATFORM, user_id))
        for user_id in corpus.VIEWERS
    }


def embeddings(api_key: str) -> OpenAICompatibleEmbeddings:
    return OpenAICompatibleEmbeddings(
        api_key=api_key,
        base_url=BASE_URL,
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
    )


# --- seeding -------------------------------------------------------------


async def reset(engine: AsyncEngine) -> None:
    """Empty every table, derived from the catalog rather than listed."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                DO $$
                DECLARE tables text;
                BEGIN
                    SELECT string_agg(format('%I.%I', schemaname, tablename), ', ')
                      INTO tables
                      FROM pg_tables
                     WHERE schemaname = 'public'
                       AND tablename <> 'alembic_version';
                    IF tables IS NOT NULL THEN
                        EXECUTE 'TRUNCATE ' || tables || ' RESTART IDENTITY CASCADE';
                    END IF;
                END $$;
                """
            )
        )


async def register_channels(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for channel_id, name in (
            (corpus.GENERAL, "general"),
            (corpus.ENGINEERING, "engineering"),
            (corpus.LEADERSHIP, "leadership"),
            (corpus.DESIGN, "design"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT DO NOTHING"
                ),
                {"id": channel_id, "n": name},
            )


@dataclass(frozen=True, slots=True)
class SeededCorpus:
    store: PostgresStore
    search: HybridSearch
    messages: int
    windows: int
    embedded: int


async def seed(engine: AsyncEngine, api_key: str) -> SeededCorpus:
    """Capture, window, embed -- the ingest process's three jobs, in order."""
    await reset(engine)
    await register_channels(engine)

    store = PostgresStore(engine)
    service = IngestService(
        source=UnusedSource(),  # type: ignore[arg-type]
        store=store,
        windows=WindowBuilder(),
        indexed_channels=corpus.INDEXED,
    )

    captured = 0
    for message in corpus.messages():
        if await service.capture(message):
            captured += 1

    windows = await _build_all_windows(service, store)
    client = embeddings(api_key)
    embedded = await _drain_embeddings(EmbeddingWorker(store, client))

    log.info(
        "goldens.seeded", messages=captured, windows=windows, embedded=embedded
    )
    return SeededCorpus(
        store=store,
        search=HybridSearch(engine, client),
        messages=captured,
        windows=windows,
        embedded=embedded,
    )


async def _build_all_windows(service: IngestService, store: Store) -> int:
    total = 0
    while rebuilt := await rebuild_pending_windows(service, store, batch=500):
        total += rebuilt
    return total


async def _drain_embeddings(worker: EmbeddingWorker) -> int:
    total = 0
    while done := await worker.run_once():
        total += done
    return total


# --- sweeping ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegOutcome:
    """Which half of the hybrid could answer a question on its own."""

    question_id: str
    declared: Leg
    lexical_found: bool
    vector_found: bool


@dataclass(frozen=True, slots=True)
class Sweep:
    report: GoldenReport
    legs: tuple[LegOutcome, ...]
    corpus: SeededCorpus


def _ranked(hits: Sequence[SearchHit]) -> tuple[RankedResult, ...]:
    """A search result reduced to what scoring is allowed to see.

    The text and the score are dropped here rather than ignored downstream: a
    metric that can read the ranker's own score is one that can be tuned to
    agree with it.
    """
    return tuple(
        RankedResult(
            window_id=hit.window_id,
            channel_id=hit.channel.platform_channel_id,
            message_ids=tuple(hit.message_ids),
        )
        for hit in hits
    )


async def sweep(
    engine: AsyncEngine, seeded: SeededCorpus, api_key: str, k: int = DEFAULT_K
) -> Sweep:
    """Score every question, and check every question against every viewer.

    Quality is measured once per question, for the viewer the question names.
    Permission is checked for the whole cross product, because the question
    that leaks is rarely the one somebody thought to ask as the wrong person.
    """
    viewers = await resolve_viewers()
    scores: list[QuestionScore] = []
    breaches: list[AclBreach] = []
    legs: list[LegOutcome] = []

    for question in QUESTIONS:
        for user_id, viewer in viewers.items():
            hits = await seeded.search.search(
                viewer, SearchQuery(text=question.text, limit=SEARCH_LIMIT)
            )
            ranked = _ranked(hits)
            breaches.extend(
                acl_breaches(
                    question_id=question.id,
                    viewer=corpus.DISPLAY[user_id],
                    readable_channels=corpus.readable_by(user_id),
                    message_channels=corpus.MESSAGE_CHANNELS,
                    ranked=ranked,
                )
            )
            if user_id == question.asked_by:
                scores.append(score_question(question.id, question.judgement, ranked, k=k))

        legs.append(await _leg_outcome(engine, question, viewers, api_key, k))

    return Sweep(
        report=GoldenReport(
            k=k,
            provenance=Provenance(
                measured_on=datetime.now(UTC).date().isoformat(),
                embedding_model=EMBEDDING_MODEL,
                embedding_dimensions=EMBEDDING_DIMENSIONS,
                corpus_messages=seeded.messages,
                corpus_windows=seeded.windows,
                viewers=len(viewers),
            ),
            scores=tuple(scores),
            breaches=tuple(breaches),
            notes=(
                f"searches: {len(QUESTIONS)} questions x {len(viewers)} viewers, "
                f"limit={SEARCH_LIMIT}, scored at k={k}",
            ),
        ),
        legs=tuple(legs),
        corpus=seeded,
    )


async def _leg_outcome(
    engine: AsyncEngine,
    question: GoldenQuestion,
    viewers: dict[int, Viewer],
    api_key: str,
    k: int,
) -> LegOutcome:
    """Ask each half of the hybrid on its own, for the question's own viewer.

    This is what keeps the set honest about what it is testing. A question
    declared answerable only by meaning is one thing while the corpus is
    worded as it is now, and something else after somebody rewords a message;
    running the lexical statement alone turns that from an assumption into a
    measurement.
    """
    viewer = viewers[question.asked_by]
    params = {
        "channel_ids": [c.platform_channel_id for c in viewer.visible_channels],
        "since": None,
        "until": None,
        "limit": k,
    }
    vector = (await embeddings(api_key).embed([question.text]))[0]

    async with engine.connect() as conn:
        for statement in sql.SESSION_SETUP:
            await conn.execute(text(statement))
        lexical_rows = await conn.execute(sql.LEXICAL_SEARCH, {**params, "q": question.text})
        lexical = await _with_message_ids(conn, list(lexical_rows.mappings()))
        vector_rows = await conn.execute(
            sql.VECTOR_SEARCH, {**params, "embedding": sql.vector_literal(vector)}
        )
        vectored = await _with_message_ids(conn, list(vector_rows.mappings()))

    expected = question.judgement.expected
    return LegOutcome(
        question_id=question.id,
        declared=question.leg,
        lexical_found=bool(expected & {m for r in lexical for m in r.message_ids}),
        vector_found=bool(expected & {m for r in vectored for m in r.message_ids}),
    )


async def _with_message_ids(
    conn: AsyncConnection, rows: Sequence[RowMapping]
) -> tuple[RankedResult, ...]:
    if not rows:
        return ()
    window_ids = [cast(int, r["id"]) for r in rows]
    resolved = await conn.execute(sql.WINDOW_MESSAGE_IDS, {"window_ids": window_ids})
    by_window: dict[int, list[int]] = {}
    for row in resolved.mappings():
        by_window.setdefault(cast(int, row["window_id"]), []).append(
            cast(int, row["message_id"])
        )
    return tuple(
        RankedResult(
            window_id=cast(int, r["id"]),
            channel_id=cast(int, r["channel_id"]),
            message_ids=tuple(by_window.get(cast(int, r["id"]), ())),
        )
        for r in rows
    )
