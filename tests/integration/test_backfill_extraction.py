"""The extraction backlog, against real SQL.

The unit tests prove the pass; this proves the two columns underneath it. A
fake written by the same hand as the code cannot show that `IS DISTINCT FROM`
is the pending test, that the upsert's conflict clause really bumps the
revision, or that a message read back out of the corpus brings its mentions
with it -- and mentions are most of what says who an ask fell to.

History is written here exactly as backfill writes it: `upsert_messages` and
nothing else. Nothing is submitted to the live worker, because the defect this
covers is that nothing ever was.

The model is stubbed. What is being tested is the queue, not the extractor's
judgement.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import AskCandidate, AskKind, ExtractedAsk, Extraction
from chatmemory.app.asks.resolution import ObservedDirectory
from chatmemory.app.asks.worker import BacklogExtractionWorker, ExtractionWorker
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message
from chatmemory.ports.store import PendingExtraction, Store

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"

OPEN_CH = ChannelRef(PLATFORM, 100)
OTHER_CH = ChannelRef(PLATFORM, 300)

ALICE = PersonRef(PLATFORM, 1)
BOB = PersonRef(PLATFORM, 2)

LONG_AGO = datetime(2025, 3, 2, 9, 0, tzinfo=UTC)


def message(
    message_id: int,
    author: PersonRef = ALICE,
    content: str = "can you review the migration?",
    channel: ChannelRef = OPEN_CH,
    at: datetime | None = None,
    mentions: frozenset[PersonRef] = frozenset(),
    reply_to_id: int | None = None,
) -> Message:
    return Message(
        platform_message_id=message_id,
        channel=channel,
        author=author,
        content=content,
        created_at=at or LONG_AGO,
        mentions=mentions,
        reply_to_id=reply_to_id,
        author_display="alice" if author == ALICE else "bob",
    )


class StubExtractor:
    """One request per candidate, so the queue is what varies, not the model."""

    def __init__(self) -> None:
        self.calls: list[AskCandidate] = []

    async def extract(self, candidate: AskCandidate) -> Extraction:
        self.calls.append(candidate)
        return Extraction(
            asks=(
                ExtractedAsk(
                    kind=AskKind.REQUEST, text="review the migration", confidence=0.9
                ),
            )
        )


@pytest.fixture
def store(clean: AsyncEngine) -> Store:
    return PostgresStore(clean)


async def backfilled(store: Store, *messages: Message) -> None:
    """History as backfill leaves it: written, offered to nothing."""
    await store.upsert_messages(list(messages))


# --- the queue -----------------------------------------------------------


async def test_backfilled_history_is_offered_for_extraction(store: Store) -> None:
    """The defect itself: nothing ever submitted these, so nothing read them."""
    await backfilled(store, message(10, ALICE, "@bob can you review the migration?",
                                    mentions=frozenset({BOB})))

    pending = await store.messages_pending_extraction(10, [OPEN_CH])

    assert [e.message.platform_message_id for e in pending] == [10]
    assert pending[0].message.author == ALICE
    assert pending[0].message.author_display == "alice"
    # Without these the message reads as chatter, and the ask it carries is
    # both unpaid for and unattributed.
    assert pending[0].message.mentions == frozenset({BOB})


async def test_a_recorded_message_stops_being_offered(store: Store) -> None:
    await backfilled(store, message(10))
    pending = await store.messages_pending_extraction(10, [OPEN_CH])

    assert await store.record_extraction(pending) == 1
    assert await store.messages_pending_extraction(10, [OPEN_CH]) == []


async def test_an_edit_puts_a_message_back_in_the_queue(store: Store) -> None:
    """Extraction read text that no longer exists, so it has to read it again."""
    await backfilled(store, message(10, content="can you review the migration?"))
    await store.record_extraction(await store.messages_pending_extraction(10, [OPEN_CH]))

    await backfilled(
        store,
        replace(
            message(10, content="can you review the other one?"),
            edited_at=LONG_AGO + timedelta(minutes=5),
        ),
    )

    pending = await store.messages_pending_extraction(10, [OPEN_CH])
    assert [e.message.content for e in pending] == ["can you review the other one?"]
    assert pending[0].generation == 1, "the upsert did not bump the revision"


async def test_a_mark_for_a_superseded_revision_leaves_the_message_pending(
    store: Store,
) -> None:
    """Migration 0009's race, per message: an edit landing while the model was
    being asked must not be cleared by the mark that follows it."""
    await backfilled(store, message(10))
    read = await store.messages_pending_extraction(10, [OPEN_CH])

    # The edit arrives while the batch is out with the extractor.
    await backfilled(
        store,
        replace(
            message(10, content="actually, never mind"),
            edited_at=LONG_AGO + timedelta(minutes=5),
        ),
    )
    await store.record_extraction(read)

    still_pending = await store.messages_pending_extraction(10, [OPEN_CH])
    assert [e.message.content for e in still_pending] == ["actually, never mind"]


async def test_the_live_path_records_without_a_generation(store: Store) -> None:
    """Capture hands the live worker a message rather than a row, so it has no
    revision to name -- and must still be able to say it has read it, or the
    backlog pass pays for every live message a second time."""
    await backfilled(store, message(10))

    assert await store.record_extraction([PendingExtraction(message=message(10))]) == 1
    assert await store.messages_pending_extraction(10, [OPEN_CH]) == []


async def test_a_deleted_message_is_never_offered(store: Store) -> None:
    """Deleted content stops being returned everywhere, including to workers."""
    await backfilled(store, message(10))
    await store.tombstone_message(10, datetime.now(UTC))

    assert await store.messages_pending_extraction(10, [OPEN_CH]) == []
    assert await store.pending_extraction_count(channels=[OPEN_CH]) == 0


async def test_a_channel_that_left_indexing_scope_is_not_read(
    store: Store, clean: AsyncEngine
) -> None:
    """Scope is the operator's configured channel list, not a stored column.

    `channel.is_indexed` is written TRUE when a channel is first seen and
    never written again by anything, so a predicate on it excludes nothing --
    a channel taken out of scope would go on being read and paid for on every
    pass. Binding the configured ids is what actually narrows it.
    """
    await backfilled(store, message(10, channel=OPEN_CH), message(11, channel=OTHER_CH))

    # Only OTHER_CH is configured now; OPEN_CH has left scope.
    pending = await store.messages_pending_extraction(10, [OTHER_CH])

    assert [e.message.platform_message_id for e in pending] == [11]
    # The health figure reads on the same predicate, or it reports a backlog
    # that is never going to drain.
    assert await store.pending_extraction_count(channels=[OTHER_CH]) == 1


async def test_nothing_is_read_when_no_channel_is_configured(
    store: Store, clean: AsyncEngine
) -> None:
    """Indexing is opt-in, so an empty scope means nothing rather than all."""
    await backfilled(store, message(10, channel=OPEN_CH))

    assert await store.messages_pending_extraction(10, []) == []
    assert await store.pending_extraction_count(channels=[]) == 0


async def test_a_message_said_moments_ago_is_left_to_the_live_pass(store: Store) -> None:
    """It is probably still buffered in the live worker's window. Reading it
    here as well buys the same model call twice."""
    await backfilled(store, message(10, at=datetime.now(UTC)))

    assert await store.messages_pending_extraction(10, [OPEN_CH]) == []
    assert await store.pending_extraction_count(channels=[OPEN_CH]) == 0


async def test_the_backlog_count_is_capped(store: Store) -> None:
    await backfilled(
        store, *(message(1000 + n, at=LONG_AGO + timedelta(minutes=n)) for n in range(5))
    )
    assert await store.pending_extraction_count(cap=2, channels=[OPEN_CH]) == 2
    assert await store.pending_extraction_count(cap=100, channels=[OPEN_CH]) == 5


async def test_the_newest_history_is_offered_first(store: Store) -> None:
    await backfilled(
        store,
        message(10, at=LONG_AGO),
        message(11, at=LONG_AGO + timedelta(days=200)),
    )
    pending = await store.messages_pending_extraction(1, [OPEN_CH])
    assert [e.message.platform_message_id for e in pending] == [11]


# --- the whole path ------------------------------------------------------


async def test_history_becomes_an_ask_row_through_the_backlog_worker(
    clean: AsyncEngine, store: Store
) -> None:
    """Corpus to `ask` row with nothing streamed: the feature, end to end."""
    asks = PostgresAskStore(clean)
    directory = ObservedDirectory()
    extractor = StubExtractor()
    worker = BacklogExtractionWorker(
        ExtractionWorker(
            ExtractionService(extractor=extractor, store=asks, directory=directory),
            directory=directory,
        ),
        store,
        channels=[OPEN_CH],
    )

    await backfilled(
        store,
        message(10, ALICE, "@bob can you review the migration?", mentions=frozenset({BOB})),
    )

    assert await worker.run_once() == 1

    async with clean.connect() as conn:
        rows = await conn.execute(text("SELECT source_message_id FROM ask"))
        assert [int(r[0]) for r in rows] == [10]

    # And it is not paid for again.
    assert await worker.run_once() == 0
    assert len(extractor.calls) == 1
    assert worker.progress.pending == 0
