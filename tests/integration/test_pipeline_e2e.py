"""The whole pipeline, from a platform event to an answer, with nothing faked
between them.

Every layer here was already covered in isolation when the system captured
messages and produced nothing retrievable: the store silently lacked the
methods that build windows and store embeddings, so ingestion ran, health
checks stayed green, and retrieval returned nothing forever. No test spanned
the joins, which is exactly where the defect lived.

So this file asserts the joins. The only fakes are the Discord objects at the
very edge -- the raw messages the gateway hands over, the REST history pages,
and the guild whose permissions are resolved. Between them runs the real
ingest service, the real window builder, the real Postgres store, the real
embedding client, and the real permission-filtered search:

    ingest -> window -> embed -> retrieve -> answer -> forget

Each step's only input is the previous step's output in the database, so a
step that silently does nothing fails the chain here rather than in
production.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest_asyncio
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.acl import DiscordAclResolver, static_guild
from chatmemory.adapters.discord.gateway import GatewayEventHandler
from chatmemory.adapters.discord.source import (
    DiscordChatSource,
    DiscordHistoryReader,
    RawMessage,
)
from chatmemory.adapters.llm.embeddings import OpenAICompatibleEmbeddings
from chatmemory.adapters.store import sql
from chatmemory.adapters.store.postgres import HybridSearch, PostgresStore
from chatmemory.app.ask import AskRequest, AskService
from chatmemory.app.ingest import EmbeddingWorker, IngestService
from chatmemory.app.reasoning.retrieval import CorpusRetrieval, discord_urls
from chatmemory.app.windowing import WindowBuilder
from chatmemory.composition import build_answers, build_ask_service, build_chat_model
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import SearchQuery
from chatmemory.entrypoints.ingest import rebuild_pending_windows
from chatmemory.ports.store import Store
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember
from tests.unit.test_source import Author, Chan, raw

# No module-level asyncio mark: `asyncio_mode = auto` marks the coroutine
# tests, and this file also holds a synchronous structural check that the mark
# would warn about.
log = structlog.get_logger()

PLATFORM = "discord"
GUILD = 7
GENERAL, LEADERSHIP = 100, 300
INDEXED = frozenset({GENERAL, LEADERSHIP})
LEAD, STAFF = 1, 3
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

EMBEDDING_MODEL = "text-embedding-3-small"
DIMS = 1536
# The cheap handle: this file asks real questions, and the stages it reaches
# need schema-constrained output rather than a frontier model.
CHAT_MODEL = "gpt-4o-mini"

# One conversation per channel, worded so that the questions asked below share
# no significant word with either. Retrieval therefore has to match on meaning
# or not at all, which is the only reason vector search exists alongside
# lexical search -- and a lexical assertion below proves the distinction is
# real rather than assumed.
GENERAL_HISTORY = (
    (1001, LEAD, "the espresso machine in the kitchen stopped working this morning"),
    (1002, STAFF, "a technician is booked for thursday to repair it"),
)
GENERAL_LIVE = (1003, STAFF, "until then there is instant in the cupboard by the sink")
LEADERSHIP_HISTORY = (
    (3001, LEAD, "we are moving the whole team into the riverside building in march"),
    (3002, LEAD, "the lease on the current floor ends on the last day of february"),
)

COFFEE_QUESTION = "when will the broken coffee maker be fixed"
RELOCATION_QUESTION = "are we relocating to a different address soon"
# Words that appear only in the channel the restricted viewer cannot read.
LEADERSHIP_WORDS = ("riverside", "february", "lease", "march")


# --- the Discord edge ---------------------------------------------------


class FakeHistory:
    """One channel's REST history, newest first, as discord.py yields it."""

    def __init__(self, messages: Sequence[RawMessage]) -> None:
        self._messages = tuple(messages)

    def history(
        self, *, limit: int, before: object | None, oldest_first: bool
    ) -> AsyncIterator[RawMessage]:
        # One page, shorter than the limit: that is what tells the backfill
        # loop the channel is complete rather than merely exhausted for now.
        page = () if before is not None else self._messages

        async def pages() -> AsyncIterator[RawMessage]:
            for message in sorted(page, key=lambda m: m.id, reverse=True):
                yield message

        return pages()


@dataclass(frozen=True, slots=True)
class RawDelete:
    """`on_raw_message_delete`'s payload: the only event fired for a message
    discord.py never cached, which after a restart is all of them."""

    message_id: int
    channel_id: int


def platform_message(
    mid: int, author: int, content: str, channel: int, minutes: int
) -> RawMessage:
    return raw(
        mid,
        content=content,
        created_at=T0 + timedelta(minutes=minutes),
        author=Author(author),
        channel=Chan(channel),
    )


def guild() -> FakeGuild:
    """The permission source, and the only Discord object the ACL layer sees."""
    return FakeGuild(
        id=GUILD,
        members=[
            FakeMember(LEAD, frozenset({"lead"})),
            FakeMember(STAFF, frozenset()),
        ],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(LEADERSHIP, allowed_roles=frozenset({"lead"})),
        ],
    )


async def viewer_for(user_id: int) -> Viewer:
    """Resolved through the real ACL adapter, never constructed by hand.

    A viewer assembled in the test would prove only that the SQL filters by
    whatever it was handed; resolving it here means the channel set under test
    is the one the bot would actually compute for that person.
    """
    resolver = DiscordAclResolver(static_guild(guild()), INDEXED)
    return await resolver.resolve_viewer(PersonRef(PLATFORM, user_id))


# --- the pipeline under test -------------------------------------------


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Everything the ingest process wires, plus what one full pass produced."""

    engine: AsyncEngine
    store: PostgresStore
    source: DiscordChatSource
    service: IngestService
    handler: GatewayEventHandler
    embeddings: OpenAICompatibleEmbeddings
    worker: EmbeddingWorker
    retrieval: CorpusRetrieval
    ingested: int
    windows: int
    embedded: int


async def register_channels(engine: AsyncEngine) -> None:
    """Indexing scope as the operator declares it, before any message arrives."""
    async with engine.begin() as conn:
        for cid, name in ((GENERAL, "general"), (LEADERSHIP, "leadership")):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT DO NOTHING"
                ),
                {"id": cid, "n": name},
            )


async def drain_live(source: DiscordChatSource, service: IngestService) -> int:
    """Persist what the gateway buffered, the way `live_loop` does."""
    stream = source.stream()
    captured = 0
    for _ in range(source.pending):
        if await service.capture(await anext(stream)):
            captured += 1
    await stream.aclose()
    return captured


async def build_all_windows(service: IngestService, store: Store) -> int:
    """Run the window loop to quiescence and report what it formed."""
    total = 0
    while rebuilt := await rebuild_pending_windows(service, store, batch=500):
        total += rebuilt
    return total


async def drain_embeddings(worker: EmbeddingWorker) -> int:
    total = 0
    while done := await worker.run_once():
        total += done
    return total


@pytest_asyncio.fixture
async def pipeline(clean: AsyncEngine, openai_key: str) -> Pipeline:
    """One full pass: backfill and live capture, then windows, then vectors."""
    await register_channels(clean)
    store = PostgresStore(clean)

    histories = {
        GENERAL: FakeHistory(
            [
                platform_message(mid, author, body, GENERAL, minutes)
                for minutes, (mid, author, body) in enumerate(GENERAL_HISTORY)
            ]
        ),
        LEADERSHIP: FakeHistory(
            [
                platform_message(mid, author, body, LEADERSHIP, minutes)
                for minutes, (mid, author, body) in enumerate(LEADERSHIP_HISTORY)
            ]
        ),
    }
    source = DiscordChatSource(DiscordHistoryReader(histories.get))
    service = IngestService(
        source=source,
        store=store,
        windows=WindowBuilder(),
        indexed_channels=INDEXED,
    )
    handler = GatewayEventHandler(sink=service, feed=source)

    ingested = 0
    for channel_id in (GENERAL, LEADERSHIP):
        ingested += await service.backfill_channel(ChannelRef(PLATFORM, channel_id))
    await handler.on_message(
        platform_message(*GENERAL_LIVE, channel=GENERAL, minutes=len(GENERAL_HISTORY))
    )
    ingested += await drain_live(source, service)

    windows = await build_all_windows(service, store)

    embeddings = OpenAICompatibleEmbeddings(
        api_key=openai_key,
        base_url="https://api.openai.com/v1",
        model=EMBEDDING_MODEL,
        dimensions=DIMS,
    )
    worker = EmbeddingWorker(store, embeddings)
    embedded = await drain_embeddings(worker)

    log.info(
        "pipeline.e2e.ingested",
        messages=ingested,
        windows=windows,
        embeddings=embedded,
    )
    return Pipeline(
        engine=clean,
        store=store,
        source=source,
        service=service,
        handler=handler,
        embeddings=embeddings,
        worker=worker,
        retrieval=CorpusRetrieval(
            HybridSearch(clean, embeddings), discord_urls(GUILD)
        ),
        ingested=ingested,
        windows=windows,
        embedded=embedded,
    )


# --- inspection helpers -------------------------------------------------


async def scalar(engine: AsyncEngine, statement: str) -> int:
    async with engine.connect() as conn:
        row = await conn.execute(text(statement))
        return int(row.scalar_one())


async def live_window_texts(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT text FROM conversation_window "
                "WHERE deleted_at IS NULL ORDER BY starts_at"
            )
        )
        return [str(r[0]) for r in rows]


async def lexical_texts(engine: AsyncEngine, viewer: Viewer, query: str) -> list[str]:
    """What the lexical leg alone would return, for the same viewer."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            sql.LEXICAL_SEARCH,
            {
                "q": query,
                "channel_ids": [c.platform_channel_id for c in viewer.visible_channels],
                "since": None,
                "until": None,
                "limit": 50,
            },
        )
        return [str(r["text"]) for r in rows.mappings()]


def ask_service(pipeline: Pipeline, key: str) -> AskService:
    """The graph `bot.main` composes, over the corpus just ingested."""
    config = settings(key)
    answers = build_answers(pipeline.retrieval, build_chat_model(config))
    return build_ask_service(config, static_guild(guild()), answers)


def settings(key: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        discord_token="unused-in-this-test",
        discord_guild_id=GUILD,
        # The corpus is reached through the engine the fixture already holds;
        # nothing on the answer path opens a pool from this value.
        database_url="postgresql+asyncpg://unused/unused",
        llm_api_key=key,
        indexed_channel_ids=f"{GENERAL} {LEADERSHIP}",
        chat_model=CHAT_MODEL,
    )


# --- 1-3: capture, windowing, embedding ---------------------------------


async def test_ingestion_leaves_every_message_windowed_and_every_window_embedded(
    pipeline: Pipeline,
) -> None:
    """The defect's exact signature, stated as a query.

    Messages present, windows absent or vectors absent is precisely the state
    the system shipped in: healthy, ingesting, and unable to answer anything.
    """
    assert pipeline.ingested == len(GENERAL_HISTORY) + 1 + len(LEADERSHIP_HISTORY)
    assert pipeline.windows == 2  # one conversation per channel
    assert pipeline.embedded == pipeline.windows

    assert await scalar(pipeline.engine, "SELECT count(*) FROM message") == 5
    assert (
        await scalar(
            pipeline.engine,
            "SELECT count(*) FROM conversation_window WHERE deleted_at IS NULL",
        )
        == 2
    )
    assert (
        await scalar(
            pipeline.engine,
            "SELECT count(*) FROM conversation_window "
            "WHERE deleted_at IS NULL AND embedding IS NULL",
        )
        == 0
    ), "a window without a vector is a conversation retrieval cannot reach"

    # The live message travelled the gateway path, not the history path, and
    # still landed inside its channel's window.
    general = next(t for t in await live_window_texts(pipeline.engine) if "espresso" in t)
    assert GENERAL_LIVE[2] in general

    assert await pipeline.store.messages_without_window(10) == []
    assert await pipeline.store.windows_missing_embeddings(10) == []


# --- 4: a permitted viewer retrieves by meaning -------------------------


async def test_a_permitted_viewer_finds_the_conversation_by_paraphrase(
    pipeline: Pipeline,
) -> None:
    """Matching on wording the corpus does not contain.

    The lexical assertion is what gives this test its meaning: without it a
    pass proves only that something matched, and term overlap would have been
    enough. With it, the hit can only have come from the vector leg -- which
    is the half of retrieval that silently does nothing when embeddings are
    missing.
    """
    viewer = await viewer_for(STAFF)
    assert await lexical_texts(pipeline.engine, viewer, COFFEE_QUESTION) == [], (
        "the question shares terms with the corpus; it no longer tests "
        "semantic retrieval"
    )

    started = time.perf_counter()
    result = await pipeline.retrieval.retrieve(viewer, SearchQuery(text=COFFEE_QUESTION))
    elapsed_ms = (time.perf_counter() - started) * 1000
    log.info("pipeline.e2e.retrieval", milliseconds=round(elapsed_ms, 1))

    assert result.items, "nothing retrievable: the pipeline produced no reachable vector"
    assert "espresso" in result.items[0].text
    assert result.items[0].channel == ChannelRef(PLATFORM, GENERAL)
    # Only the guild and channel are asserted: `HybridSearch` does not resolve
    # a window's message ids today, so a citation deep-links to the
    # conversation rather than to the exact message. Asserting the message
    # segment either way would freeze that in place.
    assert result.items[0].url.startswith(f"https://discord.com/channels/{GUILD}/{GENERAL}")


async def test_a_privileged_viewer_reaches_the_restricted_channel_the_same_way(
    pipeline: Pipeline,
) -> None:
    """The other half of the ACL claim: the content is there to be found.

    Without this, the restricted viewer's empty result below would also pass
    against a corpus that simply never indexed the channel.
    """
    result = await pipeline.retrieval.retrieve(
        await viewer_for(LEAD), SearchQuery(text=RELOCATION_QUESTION)
    )
    assert any(e.channel == ChannelRef(PLATFORM, LEADERSHIP) for e in result.items)
    assert any("riverside" in e.text for e in result.items)


# --- 5: a viewer without access gets nothing at all ---------------------


async def test_a_viewer_without_access_gets_no_content_no_excerpt_and_no_count(
    pipeline: Pipeline,
) -> None:
    """Not "fewer results" -- none, and no sign that anything was held back."""
    viewer = await viewer_for(STAFF)
    assert viewer.visible_channels == {ChannelRef(PLATFORM, GENERAL)}

    result = await pipeline.retrieval.retrieve(viewer, SearchQuery(text=RELOCATION_QUESTION))

    assert all(e.channel == ChannelRef(PLATFORM, GENERAL) for e in result.items)
    body = " ".join(e.text for e in result.items).lower()
    excerpts = " ".join(e.citation().excerpt for e in result.items).lower()
    for word in LEADERSHIP_WORDS:
        assert word not in body
        assert word not in excerpts
    # The count-only signal is a disclosure of its own: it tells the asker
    # that something they may not read exists and matched.
    assert not result.access_blocked


async def test_the_restricted_channels_own_words_still_return_nothing(
    pipeline: Pipeline,
) -> None:
    """The lexical leg searched with the exact wording, which is where a
    post-filter would leak: the ranking would find it and the filter would
    then have to remove it."""
    viewer = await viewer_for(STAFF)
    query = "riverside building lease march"
    assert await lexical_texts(pipeline.engine, viewer, query) == []

    result = await pipeline.retrieval.retrieve(viewer, SearchQuery(text=query))
    assert all("riverside" not in e.text for e in result.items)


# --- 6: deletion, everywhere, immediately -------------------------------


async def test_deleting_a_message_withdraws_it_and_the_window_that_held_it(
    pipeline: Pipeline,
) -> None:
    """A tombstone has to reach the projection, not just the message row.

    The deleted line lives inside a window whose text was embedded as a
    whole, so leaving that window live would keep serving the retracted words
    inside a hit that no longer cites them.
    """
    viewer = await viewer_for(STAFF)
    assert await lexical_texts(pipeline.engine, viewer, "cupboard instant sink")

    await pipeline.handler.on_raw_message_delete(RawDelete(GENERAL_LIVE[0], GENERAL))

    # Immediately, with no rebuild in between: the window is gone from every
    # read path the moment the event is handled.
    assert await lexical_texts(pipeline.engine, viewer, "cupboard instant sink") == []
    assert await lexical_texts(pipeline.engine, viewer, "espresso machine kitchen") == []
    withdrawn = await pipeline.retrieval.retrieve(viewer, SearchQuery(text=COFFEE_QUESTION))
    assert all("espresso" not in e.text for e in withdrawn.items)

    # The surviving neighbours come back through the rebuild, without it.
    rebuilt = await build_all_windows(pipeline.service, pipeline.store)
    assert rebuilt == 1
    await drain_embeddings(pipeline.worker)

    general = next(t for t in await live_window_texts(pipeline.engine) if "espresso" in t)
    assert "cupboard" not in general
    result = await pipeline.retrieval.retrieve(viewer, SearchQuery(text=COFFEE_QUESTION))
    assert result.items and "espresso" in result.items[0].text
    assert all("cupboard" not in e.text for e in result.items)


async def test_a_re_ingest_never_resurrects_a_tombstone(pipeline: Pipeline) -> None:
    """Backfill and reconciliation both re-read history pages written before a
    deletion. Re-capturing one must change nothing, or every sweep undoes the
    deletions made since the last one."""
    viewer = await viewer_for(STAFF)
    await pipeline.handler.on_raw_message_delete(RawDelete(GENERAL_LIVE[0], GENERAL))
    await build_all_windows(pipeline.service, pipeline.store)

    await pipeline.handler.on_message(
        platform_message(*GENERAL_LIVE, channel=GENERAL, minutes=len(GENERAL_HISTORY))
    )
    assert await drain_live(pipeline.source, pipeline.service) == 1
    await build_all_windows(pipeline.service, pipeline.store)
    await drain_embeddings(pipeline.worker)

    assert await lexical_texts(pipeline.engine, viewer, "cupboard instant sink") == []
    assert all("cupboard" not in t for t in await live_window_texts(pipeline.engine))
    result = await pipeline.retrieval.retrieve(viewer, SearchQuery(text=COFFEE_QUESTION))
    assert all("cupboard" not in e.text for e in result.items)


# --- through to an answer -----------------------------------------------


async def test_the_composed_bot_answers_from_the_corpus_it_just_ingested(
    pipeline: Pipeline, openai_key: str
) -> None:
    """The last join: the graph `bot.main` builds, over this corpus, answering
    a question whose words appear nowhere in it."""
    outcome = await ask_service(pipeline, openai_key).ask(
        AskRequest(PersonRef(PLATFORM, STAFF), COFFEE_QUESTION, None, location_id=STAFF)
    )

    assert outcome.scoped is not None
    answer = outcome.scoped.answer
    assert not answer.abstained, answer.text
    assert {c.channel for c in answer.citations} == {ChannelRef(PLATFORM, GENERAL)}
    assert "thursday" in answer.text.lower(), answer.text


async def test_the_answer_path_tells_a_restricted_asker_nothing(
    pipeline: Pipeline, openai_key: str
) -> None:
    """Asked in a direct message, so the audience is the asker themselves:
    whatever comes back is bounded by their own access and nothing else."""
    outcome = await ask_service(pipeline, openai_key).ask(
        AskRequest(
            PersonRef(PLATFORM, STAFF), RELOCATION_QUESTION, None, location_id=STAFF
        )
    )

    assert outcome.scoped is not None
    scoped = outcome.scoped
    assert all(c.channel == ChannelRef(PLATFORM, GENERAL) for c in scoped.answer.citations)
    spoken = (
        scoped.answer.text + " " + " ".join(c.excerpt for c in scoped.answer.citations)
    ).lower()
    for word in LEADERSHIP_WORDS:
        assert word not in spoken, scoped.answer.text
    # Nothing to notify about: a notice that a fuller answer exists is itself
    # a disclosure to someone who may not read the evidence behind it.
    assert scoped.withheld_from_audience == frozenset()
    assert not scoped.should_notify_asker


# --- the structural check that catches the next one ---------------------


def _parameters(function: object) -> tuple[str, ...]:
    return tuple(inspect.signature(function).parameters)  # type: ignore[arg-type]


def test_the_postgres_store_never_drifts_from_the_store_port() -> None:
    """A missing method is silent; a structural check is not.

    The outage was one absent method that no test named, so naming that
    method here would only cover the instance. This enumerates the port
    instead: anything added to `Store` and not implemented on `PostgresStore`
    -- or implemented under a different signature -- fails here, before it can
    fail as an empty corpus in production.
    """
    required = {
        name: member
        for name, member in inspect.getmembers(Store, inspect.isfunction)
        if not name.startswith("_")
    }
    # The check must not pass by finding nothing to check.
    assert {"messages_without_window", "replace_windows", "windows_missing_embeddings",
            "store_embedding"} <= set(required)

    missing = sorted(n for n in required if not callable(getattr(PostgresStore, n, None)))
    assert not missing, f"PostgresStore does not implement its port: {missing}"

    mismatched = {
        name: (_parameters(member), _parameters(getattr(PostgresStore, name)))
        for name, member in required.items()
        if _parameters(member) != _parameters(getattr(PostgresStore, name))
    }
    assert not mismatched, f"PostgresStore signatures diverge from the port: {mismatched}"
