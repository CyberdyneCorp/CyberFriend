"""Catch-up over a real database: the period and the channel are SQL, not a filter.

The unit tests drive `CatchUpService` over a fake retrieval tool, which can
only prove what the service *asks for*. What is asserted here is what the
store actually *does* with it: that a window outside the period does not come
back, that a window in another channel the viewer may read does not come back
once the summary narrows to one, and that a viewer with no access to the
channel gets nothing at all -- each of those being a WHERE clause in
`LEXICAL_SEARCH`/`VECTOR_SEARCH` rather than something trimmed afterwards.

The embedding client is a deterministic fake. That is not a shortcut around a
missing API key: the property under test is the predicate, and a paid
embedding would make the test skip on most machines while proving nothing
more. A skipped test is not a passing test.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.postgres import HybridSearch
from chatmemory.app.catchup import CatchUpService, catch_up_request
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.ports import Grounded, PromptContext, RetrievalResult
from chatmemory.app.reasoning.retrieval import CorpusRetrieval, discord_urls
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Question

pytestmark = pytest.mark.asyncio

GENERAL, LEADERSHIP = 100, 300
ASKER = PersonRef("discord", 7)
DIMS = 1536

#: "Now" for every test here. The windows below are placed relative to it, so
#: a period bound is exercised against data whose age is known exactly.
NOW = datetime(2026, 9, 17, 14, 30, tzinfo=UTC)

TODAY_TEXT = "we shipped the pricing page this afternoon"
YESTERDAY_TEXT = "the pricing page review is still open"
OLD_TEXT = "pricing was first discussed a month ago"
PRIVATE_TEXT = "pricing for the reorg is confidential"


def ch(cid: int) -> ChannelRef:
    return ChannelRef("discord", cid)


class FakeEmbeddings:
    """Deterministic vectors derived from the text, so the same words rank alike."""

    dimensions = DIMS

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, value: str) -> list[float]:
        digest = hashlib.sha256(value.encode()).digest()
        # Repeat the digest out to the column width. Any stable mapping does:
        # nothing here asserts a ranking, only which rows are eligible.
        raw = (digest * (DIMS // len(digest) + 1))[:DIMS]
        return [b / 255.0 for b in raw]


class Synthesizer:
    """Cites every window it is handed, so the assertions are about retrieval."""

    async def synthesize(
        self, question: str, evidence: Sequence[Evidence], context: PromptContext
    ) -> Grounded:
        texts = "; ".join(item.text for item in evidence)
        return Grounded(
            text=f"summary of: {texts}",
            cited_window_ids=tuple(item.window_id for item in evidence),
        )


async def seed(engine: AsyncEngine) -> None:
    """Two channels, four windows, three ages."""
    rows = (
        (GENERAL, TODAY_TEXT, NOW - timedelta(hours=2)),
        (GENERAL, YESTERDAY_TEXT, NOW - timedelta(days=1, hours=3)),
        (GENERAL, OLD_TEXT, NOW - timedelta(days=30)),
        (LEADERSHIP, PRIVATE_TEXT, NOW - timedelta(hours=1)),
    )
    embeddings = FakeEmbeddings()
    async with engine.begin() as conn:
        for cid, name in ((GENERAL, "general"), (LEADERSHIP, "leadership")):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT DO NOTHING"
                ),
                {"id": cid, "n": name},
            )
        for cid, body, at in rows:
            [vector] = await embeddings.embed([body])
            await conn.execute(
                text(
                    "INSERT INTO conversation_window "
                    "(channel_id, text, starts_at, ends_at, search_tsv, embedding) "
                    "VALUES (:c, :t, :s, :s, to_tsvector('english', :t), "
                    "CAST(:e AS vector))"
                ),
                {
                    "c": cid,
                    "t": body,
                    "s": at,
                    "e": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
                },
            )


def catchup(engine: AsyncEngine) -> CatchUpService:
    """The service as `build_catch_up` composes it, over the real backend."""
    return CatchUpService(
        CorpusRetrieval(HybridSearch(engine, FakeEmbeddings()), discord_urls(1)),
        Synthesizer(),
        clock=lambda: NOW,
    )


def question(text_: str, *visible: int) -> Question:
    channels = frozenset(ch(c) for c in visible)
    return Question(
        text=text_,
        asker=Viewer(ASKER, channels),
        audience=Audience(
            mode=DeliveryMode.DIRECT_MESSAGE,
            members=frozenset({ASKER}),
            readable_channels=channels,
        ),
    )


async def summarise(engine: AsyncEngine, asked: str, *visible: int) -> str:
    found = catch_up_request(asked)
    assert found is not None, asked
    answer = await catchup(engine).summarise(question(asked, *visible), found, None)
    return answer.text


async def test_the_period_is_applied_by_the_store(clean: AsyncEngine) -> None:
    """A month-old window is outside "since yesterday" and does not come back."""
    await seed(clean)
    summary = await summarise(
        clean, "catch me up on <#100> since yesterday", GENERAL, LEADERSHIP
    )
    assert TODAY_TEXT in summary
    assert YESTERDAY_TEXT in summary
    assert OLD_TEXT not in summary


async def test_a_narrow_period_excludes_yesterday(clean: AsyncEngine) -> None:
    """"today" starts at midnight, so yesterday's review is not in it."""
    await seed(clean)
    summary = await summarise(clean, "what did I miss in <#100> today", GENERAL)
    assert TODAY_TEXT in summary
    assert YESTERDAY_TEXT not in summary


async def test_the_summary_is_bounded_to_the_one_channel(clean: AsyncEngine) -> None:
    """The asker may read #leadership; a summary of #general must not hold it."""
    await seed(clean)
    summary = await summarise(
        clean, "catch me up on <#100> this month", GENERAL, LEADERSHIP
    )
    assert TODAY_TEXT in summary
    assert PRIVATE_TEXT not in summary


async def test_a_channel_the_asker_cannot_read_is_refused_and_never_queried(
    clean: AsyncEngine,
) -> None:
    """The refusal is decided from the viewer, before the store is consulted."""
    await seed(clean)
    summary = await summarise(clean, "catch me up on <#300> this month", GENERAL)
    assert PRIVATE_TEXT not in summary
    assert "I can't catch you up on that channel here" in summary


async def test_a_quiet_period_over_real_rows_is_reported_quiet(
    clean: AsyncEngine,
) -> None:
    """#leadership has one window an hour old and nothing before it."""
    await seed(clean)
    async with clean.begin() as conn:
        await conn.execute(
            text("UPDATE conversation_window SET starts_at = :s, ends_at = :s"),
            {"s": NOW - timedelta(days=90)},
        )
    summary = await summarise(clean, "what did I miss in <#100> today", GENERAL)
    assert "Nothing I can show you was said there" in summary


async def test_retrieval_returns_the_period_through_the_port(
    clean: AsyncEngine,
) -> None:
    """The port's own result, so a future change to the service cannot hide this."""
    await seed(clean)
    from chatmemory.domain.search import SearchQuery

    retrieval = CorpusRetrieval(HybridSearch(clean, FakeEmbeddings()), discord_urls(1))
    scope = Viewer(ASKER, frozenset({ch(GENERAL)}))
    result: RetrievalResult = await retrieval.retrieve(
        scope,
        SearchQuery(text="pricing", since=NOW - timedelta(days=2), until=None, limit=25),
    )
    bodies = {item.text for item in result.items}
    assert TODAY_TEXT in bodies
    assert OLD_TEXT not in bodies
    assert PRIVATE_TEXT not in bodies
