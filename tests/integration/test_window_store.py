"""The storage half of ingestion, against a real database.

These are the methods whose absence made the whole pipeline silently inert:
messages were captured, no window was ever built, nothing was embedded, and
retrieval returned nothing forever while every health check stayed green. The
properties asserted here are the ones that decide whether ingestion actually
produces retrievable data -- re-runnability, tombstone containment, and a
stored vector being reachable by a viewer-filtered search.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import sql
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.ingest import IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message, Window
from chatmemory.entrypoints.ingest import rebuild_pending_windows

pytestmark = pytest.mark.asyncio

CH, OTHER_CH = 100, 200
CHANNEL = ChannelRef("discord", CH)
AUTHOR = PersonRef("discord", 4242)
T0 = datetime(2026, 9, 13, tzinfo=UTC)
DIMS = 1536


def msg(mid: int, minutes: int = 0, content: str = "hello", channel: int = CH) -> Message:
    return Message(
        platform_message_id=mid,
        channel=ChannelRef("discord", channel),
        author=AUTHOR,
        content=content,
        created_at=T0 + timedelta(minutes=minutes),
    )


def window(*messages: Message, text_override: str | None = None) -> Window:
    return Window(
        channel=messages[0].channel,
        message_ids=tuple(m.platform_message_id for m in messages),
        text=text_override or " | ".join(m.content for m in messages),
        starts_at=messages[0].created_at,
        ends_at=messages[-1].created_at,
    )


async def channels(engine: AsyncEngine, *ids: int) -> None:
    async with engine.begin() as conn:
        for cid in ids:
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT DO NOTHING"
                ),
                {"id": cid, "n": f"channel-{cid}"},
            )


async def window_rows(engine: AsyncEngine) -> Sequence[tuple[int, str, int]]:
    """(id, text, member count) for every live window, oldest first."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT w.id, w.text, "
                "(SELECT count(*) FROM conversation_window_message wm "
                " WHERE wm.window_id = w.id) AS members "
                "FROM conversation_window w WHERE w.deleted_at IS NULL "
                "ORDER BY w.starts_at, w.id"
            )
        )
        return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]


async def embedding_of(engine: AsyncEngine, window_id: int) -> str | None:
    """The stored vector, as text -- the form it round-trips through."""
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT CAST(embedding AS text) FROM conversation_window WHERE id = :i"),
            {"i": window_id},
        )
        value = row.scalar()
        return None if value is None else str(value)


# --- resolve_person ----------------------------------------------------


async def test_resolve_person_creates_then_attaches(clean: AsyncEngine) -> None:
    store = PostgresStore(clean)
    first = await store.resolve_person(AUTHOR, "ada")
    second = await store.resolve_person(AUTHOR, "ada")
    assert first == second

    async with clean.connect() as conn:
        count = await conn.execute(text("SELECT count(*) FROM person"))
        assert count.scalar_one() == 1


async def test_resolve_person_writes_the_display_name_through(clean: AsyncEngine) -> None:
    """Capture seeds the name from the account id; this path knows better."""
    await channels(clean, CH)
    store = PostgresStore(clean)
    await store.upsert_messages([msg(1)])  # creates the person, name = "4242"
    person_id = await store.resolve_person(AUTHOR, "ada")

    async with clean.connect() as conn:
        name = await conn.execute(
            text("SELECT display_name FROM person WHERE id = :i"), {"i": person_id}
        )
        assert name.scalar_one() == "ada"


async def test_resolve_person_and_capture_agree_on_one_identity(
    clean: AsyncEngine,
) -> None:
    """Two paths to a person must not produce two people.

    A second person row for the same account splits that account's messages
    across identities, and every "who said this" answer is then partial.
    """
    await channels(clean, CH)
    store = PostgresStore(clean)
    resolved = await store.resolve_person(AUTHOR, "ada")
    await store.upsert_messages([msg(1)])

    async with clean.connect() as conn:
        rows = await conn.execute(text("SELECT author_person_id FROM message"))
        assert [int(r[0]) for r in rows] == [resolved]


# --- messages_without_window -------------------------------------------


async def test_pending_messages_come_back_oldest_first_and_bounded(
    clean: AsyncEngine,
) -> None:
    await channels(clean, CH)
    store = PostgresStore(clean)
    await store.upsert_messages([msg(3, 20), msg(1, 0), msg(2, 10)])

    pending = await store.messages_without_window(2)
    assert [m.platform_message_id for m in pending] == [1, 2]
    assert pending[0].author == AUTHOR  # the platform account, not the person id


async def test_a_tombstoned_message_is_never_pending(clean: AsyncEngine) -> None:
    await channels(clean, CH)
    store = PostgresStore(clean)
    await store.upsert_messages([msg(1, 0), msg(2, 1, content="retracted")])
    await store.tombstone_message(2, T0 + timedelta(hours=1))

    pending = await store.messages_without_window(10)
    assert [m.platform_message_id for m in pending] == [1]


async def test_a_windowed_message_stops_being_pending(clean: AsyncEngine) -> None:
    await channels(clean, CH)
    store = PostgresStore(clean)
    messages = [msg(1, 0), msg(2, 1)]
    await store.upsert_messages(messages)
    await store.replace_windows(CHANNEL, [window(*messages)])

    assert await store.messages_without_window(10) == []


async def test_a_deletion_returns_its_neighbours_for_rebuild(clean: AsyncEngine) -> None:
    """The tombstone case that would otherwise lose a whole conversation.

    Deleting one message withdraws the window holding it. Its surviving
    neighbours must come back through the pending list, or one deletion
    silently removes every message around it from retrieval for good.
    """
    await channels(clean, CH)
    store = PostgresStore(clean)
    messages = [msg(1, 0, content="keep me"), msg(2, 1, content="retract me")]
    await store.upsert_messages(messages)
    await store.replace_windows(CHANNEL, [window(*messages)])
    await store.tombstone_message(2, T0 + timedelta(hours=1))

    pending = await store.messages_without_window(10)
    assert [m.platform_message_id for m in pending] == [1]

    # And rebuilding from them leaves no trace of the retracted text.
    rebuilt = WindowBuilder().build(CHANNEL, pending)
    await store.replace_windows(CHANNEL, rebuilt)
    live = await window_rows(clean)
    assert len(live) == 1
    assert "retract me" not in live[0][1]
    assert await store.messages_without_window(10) == []


# --- replace_windows ---------------------------------------------------


async def test_replace_windows_run_twice_produces_no_duplicates(
    clean: AsyncEngine,
) -> None:
    await channels(clean, CH)
    store = PostgresStore(clean)
    messages = [msg(1, 0), msg(2, 1), msg(3, 40, content="later")]
    await store.upsert_messages(messages)
    windows = WindowBuilder(gap=timedelta(minutes=15)).build(CHANNEL, messages)
    assert len(windows) == 2  # the gap splits them, so this is not a trivial case

    assert await store.replace_windows(CHANNEL, windows) == 2
    first = await window_rows(clean)
    assert await store.replace_windows(CHANNEL, windows) == 2
    second = await window_rows(clean)

    assert len(second) == 2
    assert [(t, n) for _, t, n in first] == [(t, n) for _, t, n in second]

    async with clean.connect() as conn:
        members = await conn.execute(
            text("SELECT count(*) FROM conversation_window_message")
        )
        assert members.scalar_one() == 3


async def test_replace_windows_leaves_other_batches_alone(clean: AsyncEngine) -> None:
    """The loop would otherwise never converge.

    Windows are rebuilt a batch at a time. Clearing the whole channel would
    orphan every message outside the batch, putting it straight back on the
    pending list -- and re-embedding it on every pass, forever.
    """
    await channels(clean, CH)
    store = PostgresStore(clean)
    earlier, later = msg(1, 0, content="earlier"), msg(2, 600, content="later")
    await store.upsert_messages([earlier, later])

    await store.replace_windows(CHANNEL, [window(earlier)])
    await store.replace_windows(CHANNEL, [window(later)])

    assert [t for _, t, _ in await window_rows(clean)] == ["earlier", "later"]
    assert await store.messages_without_window(10) == []


async def test_replace_windows_keeps_the_vector_of_unchanged_text(
    clean: AsyncEngine,
) -> None:
    """Re-embedding text that did not change is the dominant cost here."""
    await channels(clean, CH)
    store = PostgresStore(clean)
    messages = [msg(1, 0), msg(2, 1)]
    await store.upsert_messages(messages)
    await store.replace_windows(CHANNEL, [window(*messages)])

    pending = await store.windows_missing_embeddings(10)
    assert len(pending) == 1 and pending[0].window_id is not None
    await store.store_embedding(pending[0].window_id, [0.25] * DIMS)
    stored = await embedding_of(clean, pending[0].window_id)

    await store.replace_windows(CHANNEL, [window(*messages)])
    assert await store.windows_missing_embeddings(10) == []

    # A different row carrying the same vector: the window really was
    # replaced, and the vector was carried across rather than left behind.
    rebuilt = await window_rows(clean)
    assert len(rebuilt) == 1
    assert rebuilt[0][0] != pending[0].window_id
    assert await embedding_of(clean, rebuilt[0][0]) == stored


async def test_changed_text_drops_the_stale_vector(clean: AsyncEngine) -> None:
    """An edited window must not keep a vector describing the old text."""
    await channels(clean, CH)
    store = PostgresStore(clean)
    messages = [msg(1, 0), msg(2, 1)]
    await store.upsert_messages(messages)
    await store.replace_windows(CHANNEL, [window(*messages)])

    pending = await store.windows_missing_embeddings(10)
    assert pending[0].window_id is not None
    await store.store_embedding(pending[0].window_id, [0.25] * DIMS)

    await store.replace_windows(
        CHANNEL, [window(*messages, text_override="edited to say something else")]
    )
    again = await store.windows_missing_embeddings(10)
    assert [w.text for w in again] == ["edited to say something else"]


async def test_replace_windows_is_atomic(clean: AsyncEngine) -> None:
    """A rebuild that fails must leave the old windows, never none at all.

    The second window references a message that does not exist, so its insert
    violates the foreign key. If the delete were not in the same transaction,
    the channel would come out of this with nothing retrievable in it.
    """
    await channels(clean, CH)
    store = PostgresStore(clean)
    messages = [msg(1, 0), msg(2, 1)]
    await store.upsert_messages(messages)
    await store.replace_windows(CHANNEL, [window(*messages)])
    before = await window_rows(clean)

    broken = Window(
        channel=CHANNEL,
        message_ids=(1, 2, 999999),
        text="rebuilt",
        starts_at=T0,
        ends_at=T0 + timedelta(minutes=1),
    )
    with pytest.raises(Exception):  # noqa: B017 - the driver's integrity error
        await store.replace_windows(CHANNEL, [broken])

    assert await window_rows(clean) == before


# --- windows_missing_embeddings and store_embedding --------------------


async def test_only_live_unembedded_windows_are_offered(clean: AsyncEngine) -> None:
    await channels(clean, CH)
    store = PostgresStore(clean)
    messages = [msg(1, 0), msg(2, 1), msg(3, 600, content="withdrawn")]
    await store.upsert_messages(messages)
    await store.replace_windows(CHANNEL, [window(messages[0], messages[1])])
    await store.replace_windows(CHANNEL, [window(messages[2])])
    await store.tombstone_message(3, T0 + timedelta(hours=20))

    pending = await store.windows_missing_embeddings(10)
    assert len(pending) == 1
    assert "withdrawn" not in pending[0].text
    assert pending[0].window_id is not None
    assert pending[0].channel == CHANNEL
    assert pending[0].message_ids == (1, 2)

    await store.store_embedding(pending[0].window_id, [0.1] * DIMS)
    assert await store.windows_missing_embeddings(10) == []


async def test_store_embedding_makes_a_window_reachable_by_vector_search(
    clean: AsyncEngine,
) -> None:
    """The whole point of the pipeline, asserted end to end.

    Also asserts the viewer predicate still holds on the way back: the window
    in the channel the viewer cannot read is embedded identically and must
    not appear.
    """
    await channels(clean, CH, OTHER_CH)
    store = PostgresStore(clean)
    mine = msg(1, 0, content="the deploy runbook lives in the wiki")
    theirs = msg(2, 0, content="the deploy runbook lives in the wiki", channel=OTHER_CH)
    await store.upsert_messages([mine, theirs])
    await store.replace_windows(CHANNEL, [window(mine)])
    await store.replace_windows(ChannelRef("discord", OTHER_CH), [window(theirs)])

    vector = [0.05] * DIMS
    for pending in await store.windows_missing_embeddings(10):
        assert pending.window_id is not None
        await store.store_embedding(pending.window_id, vector)

    async with clean.connect() as conn:
        for statement in sql.SESSION_SETUP:
            await conn.execute(text(statement))
        rows = await conn.execute(
            sql.VECTOR_SEARCH,
            {
                "embedding": sql.vector_literal(vector),
                "channel_ids": [CH],
                "since": None,
                "until": None,
                "limit": 10,
            },
        )
        hits = list(rows.mappings())

    assert [int(h["channel_id"]) for h in hits] == [CH]
    assert hits[0]["text"] == mine.content


async def test_a_rebuilt_window_is_reachable_by_lexical_search(
    clean: AsyncEngine,
) -> None:
    """The tsvector is written on insert, or lexical retrieval never matches."""
    await channels(clean, CH)
    store = PostgresStore(clean)
    message = msg(1, 0, content="the postmortem is scheduled for friday")
    await store.upsert_messages([message])
    await store.replace_windows(CHANNEL, [window(message)])

    async with clean.connect() as conn:
        rows = await conn.execute(
            sql.LEXICAL_SEARCH,
            {
                "q": "postmortem friday",
                "channel_ids": [CH],
                "since": None,
                "until": None,
                "limit": 10,
            },
        )
        assert [str(r["text"]) for r in rows.mappings()] == [message.content]


# --- the loop the entrypoint actually runs -----------------------------


class NoHistory:
    """Backfill is not what is under test here; windowing is."""

    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]:
        return []

    def stream(self) -> AsyncIterator[Message]:  # pragma: no cover - unused
        raise NotImplementedError


async def test_the_rebuild_loop_converges(clean: AsyncEngine) -> None:
    """The second pass must find nothing left to do.

    A pass that keeps re-forming windows it already built never goes idle and
    re-embeds the same text on every lap -- which is the dominant cost in this
    system, spent forever on work already done.
    """
    await channels(clean, CH)
    store = PostgresStore(clean)
    messages = [msg(i, minutes=i * 2, content=f"message {i}") for i in range(1, 8)]
    await store.upsert_messages(messages)

    service = IngestService(
        source=NoHistory(),
        store=store,
        windows=WindowBuilder(max_messages=3),
        indexed_channels=frozenset({CH}),
    )

    assert await rebuild_pending_windows(service, store, batch=100) > 0
    after_first = await window_rows(clean)
    assert await rebuild_pending_windows(service, store, batch=100) == 0
    assert await window_rows(clean) == after_first
