"""The decision backfill against real SQL, and the asks it re-extracts.

History the ask pass read before decisions existed is recorded as extracted,
so only a reset puts it back in front of the model. What a fake cannot prove
is which rows the reset reaches -- the window, the scope, the tombstone and
the marker are all predicates or matches over real rows -- and that the
re-extraction it causes leaves every ask already on those messages as it was:
the same key, the status observed since, and a correction outranking a pass
that no longer finds the ask.

The model is stubbed. What is being tested is the queue and the stores.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.adapters.store.decisions_postgres import PostgresDecisionStore
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import AskCandidate, AskKind, ExtractedAsk, Extraction
from chatmemory.app.asks.resolution import ObservedDirectory
from chatmemory.app.asks.worker import BacklogExtractionWorker, ExtractionWorker
from chatmemory.app.decisions.model import ExtractedDecision
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message
from chatmemory.entrypoints.decisions_backfill import backfill

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
INDEXED = ChannelRef(PLATFORM, 820)
UNINDEXED = ChannelRef(PLATFORM, 821)
ALICE = PersonRef(PLATFORM, 8201)
BOB = PersonRef(PLATFORM, 8202)
DIMS = 1536

SINCE = date(2026, 6, 1)
IN_WINDOW = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
BEFORE_WINDOW = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)

# Marker and ask: the ask is answered before the backfill.
ANSWERED = "we decided to ship friday, @bob can you review the migration?"
# Marker and ask: Bob corrects the ask, and the second pass no longer finds it.
CORRECTED = "agreed, @bob can you rotate the keys?"
# An ask with no marker, inside the window: must not be paid for again.
NO_MARKER = "@bob can you check the logs?"
# A marker before the window.
TOO_OLD = "fechado, deploy na sexta"
# A marker in a channel outside indexing scope.
OUT_OF_SCOPE = "combinado, retro quinzenal"
# A marker on a message deleted since it was extracted.
DELETED = "decidimos: @bob can you draft the post?"


@pytest.fixture(autouse=True)
async def _requires_decision_schema(clean: AsyncEngine) -> None:
    async with clean.connect() as conn:
        present = await conn.execute(text("SELECT to_regclass('public.decision')"))
        if present.scalar() is None:
            pytest.skip("decision table is missing; run `alembic upgrade head`")


class FixedEmbeddings:
    @property
    def dimensions(self) -> int:
        return DIMS

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (DIMS - 1) for _ in texts]


class Extractor:
    """Before the feature: asks only. After it: decisions too, and the model
    no longer reads an ask into CORRECTED -- a pass that disagrees with the
    first one, which is the case a correction has to survive."""

    def __init__(self) -> None:
        self.decisions = False
        self.calls: list[str] = []

    async def extract(self, candidate: AskCandidate) -> Extraction:
        content = candidate.message.content
        self.calls.append(content)
        asks = (ExtractedAsk(kind=AskKind.REQUEST, text=content, confidence=0.9),)
        if not self.decisions:
            return Extraction(asks=asks)
        decisions = (ExtractedDecision(f"decided: {content}", "release", 0.9),)
        return Extraction(asks=() if content == CORRECTED else asks, decisions=decisions)


def message(
    message_id: int,
    content: str,
    at: datetime = IN_WINDOW,
    channel: ChannelRef = INDEXED,
) -> Message:
    return Message(
        platform_message_id=message_id,
        channel=channel,
        author=ALICE,
        content=content,
        created_at=at,
        mentions=frozenset({BOB}) if "@bob" in content else frozenset(),
        author_display="alice",
    )


HISTORY = (
    message(1, ANSWERED),
    message(2, CORRECTED),
    message(3, NO_MARKER),
    message(4, TOO_OLD, at=BEFORE_WINDOW),
    message(5, OUT_OF_SCOPE, channel=UNINDEXED),
    message(6, DELETED),
)


def worker(engine: AsyncEngine, extractor: Extractor) -> BacklogExtractionWorker:
    directory = ObservedDirectory()
    service = ExtractionService(
        extractor=extractor,
        store=PostgresAskStore(engine),
        directory=directory,
        decisions=PostgresDecisionStore(engine, FixedEmbeddings()),
    )
    # Both channels, so the drain shows the backfill's scope, not the worker's.
    return BacklogExtractionWorker(
        ExtractionWorker(service, directory=directory),
        PostgresStore(engine),
        channels=[INDEXED, UNINDEXED],
    )


async def asks(engine: AsyncEngine) -> dict[str, tuple[int, str]]:
    async with engine.connect() as conn:
        found = await conn.execute(text("SELECT ask_key, source_message_id, status FROM ask"))
        return {str(r[0]): (int(r[1]), str(r[2])) for r in found}


async def extracted_before_the_feature(engine: AsyncEngine) -> Extractor:
    """History as the ask pass left it: every message read, no decision stored."""
    store = PostgresStore(engine)
    await store.upsert_messages(list(HISTORY))
    extractor = Extractor()
    await worker(engine, extractor).run_once()
    assert await store.pending_extraction_count(channels=[INDEXED, UNINDEXED]) == 0
    return extractor


async def key_of(engine: AsyncEngine, source: int) -> str:
    [key] = [k for k, (mid, _) in (await asks(engine)).items() if mid == source]
    return key


async def answer_and_correct(engine: AsyncEngine) -> None:
    """What happened to the asks between the first pass and the backfill."""
    answered, corrected = await key_of(engine, 1), await key_of(engine, 2)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE ask SET status = 'answered', closed_by = 'reply' WHERE ask_key = :k"),
            {"k": answered},
        )
        await conn.execute(
            text(
                "INSERT INTO ask_correction (ask_key, by_person_id, resolution) "
                "SELECT :k, person_id, 'not_applicable' FROM person_platform_id "
                "WHERE platform = :p AND platform_user_id = :u"
            ),
            {"k": corrected, "p": PLATFORM, "u": BOB.platform_user_id},
        )
    await PostgresStore(engine).tombstone_message(6, datetime.now(UTC))


async def test_only_live_marker_bearing_messages_inside_the_window_are_reset(
    clean: AsyncEngine,
) -> None:
    await extracted_before_the_feature(clean)
    await answer_and_correct(clean)

    report = await backfill(clean, SINCE, frozenset({INDEXED.platform_channel_id}))

    assert (report.matched, report.reset) == (2, 2)
    pending = await PostgresStore(clean).messages_pending_extraction(
        100, [INDEXED, UNINDEXED]
    )
    assert sorted(e.message.platform_message_id for e in pending) == [1, 2]


async def test_a_second_run_resets_nothing_that_is_still_pending(clean: AsyncEngine) -> None:
    await extracted_before_the_feature(clean)
    scope = frozenset({INDEXED.platform_channel_id})
    first = await backfill(clean, SINCE, scope)

    again = await backfill(clean, SINCE, scope)

    # Nothing deleted in this one, so the three in-window markers match.
    assert (first.matched, first.reset) == (3, 3)
    assert (again.matched, again.reset, again.already_pending) == (3, 0, 3)


async def test_an_empty_scope_resets_nothing(clean: AsyncEngine) -> None:
    await extracted_before_the_feature(clean)

    report = await backfill(clean, SINCE, frozenset())

    assert (report.scanned, report.reset) == (0, 0)
    assert await PostgresStore(clean).pending_extraction_count(channels=[INDEXED]) == 0


async def test_re_extraction_keeps_ask_keys_statuses_and_corrections(clean: AsyncEngine) -> None:
    extractor = await extracted_before_the_feature(clean)
    await answer_and_correct(clean)
    before = await asks(clean)
    extractor.decisions = True
    extractor.calls.clear()

    await backfill(clean, SINCE, frozenset({INDEXED.platform_channel_id}))
    assert await worker(clean, extractor).run_once() == 2

    # Only the reset messages were paid for.
    assert sorted(extractor.calls) == sorted([ANSWERED, CORRECTED])
    # Same keys, same statuses: the answered ask stays answered, and the
    # corrected one survives a pass that no longer finds it.
    assert await asks(clean) == before
    async with clean.connect() as conn:
        corrections = await conn.execute(text("SELECT ask_key FROM ask_correction"))
        assert [str(r[0]) for r in corrections] == [await key_of(clean, 2)]
        decided = await conn.execute(
            text("SELECT source_message_id FROM decision ORDER BY source_message_id")
        )
        assert [int(r[0]) for r in decided] == [1, 2]
