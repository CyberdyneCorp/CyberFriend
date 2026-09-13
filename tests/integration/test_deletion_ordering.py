"""Deletion that outruns the write it retracts, against a real database.

Live messages are published to an in-process queue and written later; a
deletion goes straight to the database. The two therefore race, and the losing
order was the dangerous one: `UPDATE message SET deleted_at ... WHERE id = :id`
matched zero rows, the queued insert landed afterwards, and the retracted
message was live for good.

Every test here forces the delete to arrive *strictly before* the insert --
the failing order, not a hopeful interleaving -- and then asks every read path
whether the content comes back.

The second half covers reconciliation, which is the only thing that notices an
edit or deletion that happened while the process was down. It needs the store
to answer `stored_revisions`; for a long time no store did, so the entrypoint
logged one line and ran without it forever.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.source import Reconciler
from chatmemory.adapters.store import sql
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.ingest import IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message
from chatmemory.entrypoints.ingest import rebuild_pending_windows

pytestmark = pytest.mark.asyncio

# Ids of their own so nothing here shares a tombstone with another file: the
# ledger deliberately outlives the message row it describes.
CH_ID = 7711
CH = ChannelRef("discord", CH_ID)
AUTHOR = PersonRef("discord", 7712)
KEEP, RETRACTED, LATER = 7_700_001, 7_700_002, 7_700_003
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
DELETED_AT = T0 + timedelta(hours=1)

SECRET = "the production database password is hunter2"
SECRET_TERMS = "production database password hunter2"


def msg(mid: int, minutes: float = 0, content: str = "hello", edited: int = 0) -> Message:
    return Message(
        platform_message_id=mid,
        channel=CH,
        author=AUTHOR,
        content=content,
        created_at=T0 + timedelta(minutes=minutes),
        edited_at=T0 + timedelta(minutes=edited) if edited else None,
    )


class NoHistory:
    """Backfill is not what these tests drive; the queue and the ledger are."""

    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]:
        return []

    def stream(self) -> object:  # pragma: no cover - unused here
        raise NotImplementedError


class FakeHistory:
    """What the platform says the channel contains *now*, newest first.

    Reconciliation's only evidence of a deletion is absence from this.
    """

    def __init__(self, *messages: Message) -> None:
        self._messages = sorted(messages, key=lambda m: -m.platform_message_id)

    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]:
        candidates = [
            m
            for m in self._messages
            if before_message_id is None or m.platform_message_id < before_message_id
        ]
        return candidates[:limit]


async def setup(engine: AsyncEngine) -> tuple[IngestService, PostgresStore]:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO channel (id, platform, name, is_indexed) "
                "VALUES (:c,'discord','general',TRUE) ON CONFLICT DO NOTHING"
            ),
            {"c": CH_ID},
        )
    store = PostgresStore(engine)
    service = IngestService(NoHistory(), store, WindowBuilder(), frozenset({CH_ID}))
    return service, store


async def settle(service: IngestService, store: PostgresStore) -> int:
    """Run the window loop to quiescence, exactly as the entrypoint does."""
    total = 0
    while rebuilt := await rebuild_pending_windows(service, store, batch=100):
        total += rebuilt
    return total


# --- every read path, asked the same question ---------------------------


async def lexical_hits(engine: AsyncEngine, query: str) -> list[str]:
    """The lexical leg, bound to a viewer who may read the whole channel.

    Scoped to the most privileged viewer on purpose: a deletion that only
    holds for people with narrow access is not a deletion.
    """
    async with engine.connect() as conn:
        rows = await conn.execute(
            sql.LEXICAL_SEARCH,
            {
                "q": query,
                "channel_ids": [CH_ID],
                "since": None,
                "until": None,
                "limit": 50,
            },
        )
        return [str(r["text"]) for r in rows.mappings()]


async def vector_hits(engine: AsyncEngine, vector: list[float]) -> list[str]:
    async with engine.connect() as conn:
        for statement in sql.SESSION_SETUP:
            await conn.execute(text(statement))
        rows = await conn.execute(
            sql.VECTOR_SEARCH,
            {
                "embedding": sql.vector_literal(vector),
                "channel_ids": [CH_ID],
                "since": None,
                "until": None,
                "limit": 50,
            },
        )
        return [str(r["text"]) for r in rows.mappings()]


async def thread_context_ids(engine: AsyncEngine, anchor: int) -> list[int]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sql.THREAD_CONTEXT,
            {"message_id": anchor, "channel_ids": [CH_ID], "limit": 50},
        )
        return [int(r["id"]) for r in rows.mappings()]


async def live_window_texts(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT text FROM conversation_window "
                "WHERE channel_id = :c AND deleted_at IS NULL ORDER BY starts_at"
            ),
            {"c": CH_ID},
        )
        return [str(r[0]) for r in rows]


async def deleted_at_of(engine: AsyncEngine, message_id: int) -> datetime | None:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT deleted_at FROM message WHERE id = :i"), {"i": message_id}
        )
        value = row.scalar()
        return None if value is None else value


async def assert_unreachable(engine: AsyncEngine, store: PostgresStore) -> None:
    """The retracted message must be gone from every path that returns content."""
    assert await deleted_at_of(engine, RETRACTED) is not None, (
        "the message landed live despite a tombstone already recorded for it"
    )
    assert await lexical_hits(engine, SECRET_TERMS) == []
    assert all(SECRET not in t for t in await live_window_texts(engine))
    assert await thread_context_ids(engine, RETRACTED) == []
    assert RETRACTED not in [
        m.platform_message_id for m in await store.messages_without_window(50)
    ]
    assert all(SECRET not in w.text for w in await store.windows_missing_embeddings(50))


# --- the race ------------------------------------------------------------


async def test_a_delete_arriving_before_its_insert_is_never_retrievable(
    clean: AsyncEngine,
) -> None:
    """The defect, in the order that produced it.

    Nothing has ever been stored under this id when the deletion is applied,
    so the tombstone UPDATE matches nothing. The insert that follows must
    still come out withdrawn.
    """
    service, store = await setup(clean)

    await service.handle_delete(RETRACTED, at=DELETED_AT, channel=CH, created_at=T0)
    await service.capture(msg(KEEP, 0, "the standup moved to nine"))
    await service.capture(msg(RETRACTED, 1, SECRET))
    await settle(service, store)

    await assert_unreachable(clean, store)
    # And the deletion took only itself: the neighbour it would have been
    # windowed with is still there.
    assert any("standup" in t for t in await live_window_texts(clean))


async def test_the_out_of_order_tombstone_survives_a_later_edit(
    clean: AsyncEngine,
) -> None:
    """The queue can also deliver an *edited* copy after the deletion.

    Backfill and reconciliation both re-read history pages written before a
    deletion, so this is the ordinary case rather than an exotic one.
    """
    service, store = await setup(clean)

    await service.handle_delete(RETRACTED, at=DELETED_AT, channel=CH, created_at=T0)
    await service.capture(msg(RETRACTED, 1, SECRET))
    await service.handle_edit(msg(RETRACTED, 1, SECRET + " (still secret)", edited=5))
    await service.capture(msg(RETRACTED, 1, SECRET))
    await settle(service, store)

    await assert_unreachable(clean, store)
    assert await lexical_hits(clean, "still secret") == []


async def test_a_window_embedded_before_the_rebuild_cannot_serve_it_either(
    clean: AsyncEngine,
) -> None:
    """The vector leg, which ranks before it filters.

    The retracted text must never reach a window that carries a vector; if it
    did, an approximate scan would rank it and only the tombstone predicate
    would stand between it and the asker.
    """
    service, store = await setup(clean)

    await service.handle_delete(RETRACTED, at=DELETED_AT, channel=CH, created_at=T0)
    await service.capture(msg(KEEP, 0, "the standup moved to nine"))
    await service.capture(msg(RETRACTED, 1, SECRET))
    await settle(service, store)

    vector = [0.05] * 1536
    pending = await store.windows_missing_embeddings(50)
    assert pending, "nothing was windowed at all; the assertion below is vacuous"
    for window in pending:
        assert window.window_id is not None
        await store.store_embedding(window.window_id, vector)

    hits = await vector_hits(clean, vector)
    assert hits, "the surviving conversation must still be retrievable"
    assert all(SECRET not in t for t in hits)


async def test_a_tombstone_for_an_unknown_id_is_not_reported_as_stored(
    clean: AsyncEngine,
) -> None:
    """Reconciliation must not rediscover a deletion it already applied.

    Were the withdrawn message still reported, every pass would delete it
    again -- marking the channel dirty and re-forming its windows forever.
    """
    service, store = await setup(clean)

    await service.handle_delete(RETRACTED, at=DELETED_AT, channel=CH, created_at=T0)
    await service.capture(msg(RETRACTED, 1, SECRET))

    revisions = await store.stored_revisions(CH, T0 - timedelta(hours=1))
    assert RETRACTED not in revisions


async def test_the_usual_order_still_withdraws_the_window_immediately(
    clean: AsyncEngine,
) -> None:
    """The ledger must not have cost the in-order case its immediacy.

    A deletion is visible on every read path the moment it is handled, with no
    rebuild in between: the window holding the message goes with it.
    """
    service, store = await setup(clean)
    await service.capture(msg(KEEP, 0, "the standup moved to nine"))
    await service.capture(msg(RETRACTED, 1, SECRET))
    await settle(service, store)
    assert await lexical_hits(clean, SECRET_TERMS)

    await service.handle_delete(RETRACTED, at=DELETED_AT, channel=CH, created_at=T0)

    assert await lexical_hits(clean, SECRET_TERMS) == []
    await settle(service, store)
    await assert_unreachable(clean, store)
    assert any("standup" in t for t in await live_window_texts(clean))


# --- reconciliation ------------------------------------------------------


async def test_stored_revisions_report_what_the_corpus_holds(
    clean: AsyncEngine,
) -> None:
    """Revisions, scoped to one channel and one window of time, never content."""
    service, store = await setup(clean)
    await service.capture(msg(KEEP, 0, "first"))
    await service.capture(msg(RETRACTED, 1, SECRET))
    await service.handle_edit(msg(LATER, 2, "amended", edited=9))

    revisions = await store.stored_revisions(CH, T0 - timedelta(minutes=1))

    assert set(revisions) == {KEEP, RETRACTED, LATER}
    assert revisions[KEEP] == T0
    # An edit moves the revision, which is the only evidence reconciliation
    # has that a message changed while we were not looking.
    assert revisions[LATER] == T0 + timedelta(minutes=9)

    # The lookback is a real boundary: absence before it is not evidence of
    # deletion, so messages older than it must not be offered for comparison.
    narrow = await store.stored_revisions(CH, T0 + timedelta(minutes=2))
    assert set(narrow) == {LATER}


async def test_a_deletion_missed_while_offline_is_repaired_by_reconciliation(
    clean: AsyncEngine,
) -> None:
    """The whole job, end to end, over the real store.

    The gateway event for this deletion was never delivered -- it fired while
    the process was down and is not replayed. Absence from a re-read of live
    history is the only evidence, and `stored_revisions` is the other half of
    the comparison.
    """
    service, store = await setup(clean)
    keep, retracted, later = (
        msg(KEEP, 0, "the standup moved to nine"),
        msg(RETRACTED, 1, SECRET),
        msg(LATER, 2, "and the room is booked"),
    )
    for message in (keep, retracted, later):
        await service.capture(message)
    await settle(service, store)
    assert await lexical_hits(clean, SECRET_TERMS), "setup did not make it findable"
    assert await store.dirty_channels() == [], "the rebuild left work outstanding"

    reconciler = Reconciler(
        source=FakeHistory(keep, later),  # the retracted one is simply gone
        ledger=store,
        sink=service,
        now=lambda: DELETED_AT,
    )
    report = await reconciler.reconcile(CH, T0 - timedelta(hours=1))

    assert report.deleted == 1
    # Marked dirty by the store, from the message row: `handle_delete` is
    # called here with an id and nothing else, so a channel that depended on
    # the caller naming it would never be re-formed -- and the neighbours
    # would stay withdrawn along with the window they shared.
    assert [c for c, _ in await store.dirty_channels()] == [CH]

    await settle(service, store)
    await assert_unreachable(clean, store)
    surviving = " ".join(await live_window_texts(clean))
    assert "standup" in surviving and "room is booked" in surviving


async def test_reconciliation_is_idempotent_once_it_has_repaired(
    clean: AsyncEngine,
) -> None:
    """A second pass must find nothing, or the loop repairs the same
    deletion on every lap and re-embeds the channel forever."""
    service, store = await setup(clean)
    keep, retracted = msg(KEEP, 0, "first"), msg(RETRACTED, 1, SECRET)
    for message in (keep, retracted):
        await service.capture(message)
    await settle(service, store)

    reconciler = Reconciler(
        source=FakeHistory(keep), ledger=store, sink=service, now=lambda: DELETED_AT
    )
    assert (await reconciler.reconcile(CH, T0 - timedelta(hours=1))).deleted == 1
    await settle(service, store)

    again = await reconciler.reconcile(CH, T0 - timedelta(hours=1))
    assert (again.updated, again.deleted) == (0, 0)
    assert await store.dirty_channels() == []
