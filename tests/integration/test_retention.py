"""Retention and opt-out against a real database.

These are the assertions that cannot be made against a fake, because the thing
being asserted is what Postgres does: that a cascade does not leave a window
holding the text of a message it just deleted, and that a re-ingest of an
opted-out person's message is refused by the database rather than by whichever
caller remembered to ask.

The opt-out half needs the schema from migration 0008. It is created and
dropped by a fixture here rather than waiting for `alembic upgrade head`, so
these run against a database at any revision -- the trigger bodies come from
the migration module itself, so the thing under test is the shipped one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import chatmemory
from chatmemory.adapters.store import sql
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.optout import OptOutService
from chatmemory.app.retention import RetentionPolicy, RetentionService
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message, Window

pytestmark = pytest.mark.asyncio

CH = 700
CHANNEL = ChannelRef("discord", CH)
ALICE = PersonRef("discord", 7001)
BOB = PersonRef("discord", 7002)

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)

MIGRATION = (
    Path(chatmemory.__file__).parent.parent.parent / "migrations" / "versions" / "0008_optout.py"
)

# Mirrors migration 0008. The trigger bodies -- the part that carries the
# behaviour -- are imported from the migration rather than copied, so a change
# there is a change here.
CREATE_OPT_OUT_TABLE = """
CREATE TABLE IF NOT EXISTS person_opt_out (
    person_id BIGINT PRIMARY KEY REFERENCES person(id) ON DELETE CASCADE,
    reason TEXT NOT NULL DEFAULT '',
    opted_out_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _load_migration() -> ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("migration_0008", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def msg(
    mid: int,
    at: datetime,
    author: PersonRef = ALICE,
    content: str = "hello",
) -> Message:
    return Message(
        platform_message_id=mid,
        channel=CHANNEL,
        author=author,
        content=content,
        created_at=at,
    )


def window(*messages: Message) -> Window:
    return Window(
        channel=CHANNEL,
        message_ids=tuple(m.platform_message_id for m in messages),
        text=" | ".join(m.content for m in messages),
        starts_at=messages[0].created_at,
        ends_at=messages[-1].created_at,
    )


async def seed_channel(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO channel (id, platform, name, is_indexed) "
                "VALUES (:id, 'discord', 'retention', TRUE) ON CONFLICT DO NOTHING"
            ),
            {"id": CH},
        )


async def message_ids(engine: AsyncEngine) -> list[int]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT id FROM message ORDER BY id"))
        return [int(r[0]) for r in rows]


async def live_window_texts(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT text FROM conversation_window "
                "WHERE deleted_at IS NULL ORDER BY starts_at"
            )
        )
        return [str(r[0]) for r in rows]


async def lexical_hits(engine: AsyncEngine, query: str) -> Sequence[str]:
    """What a viewer of this channel can actually retrieve, through the real SQL."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            sql.LEXICAL_SEARCH,
            {
                "channel_ids": [CH],
                "q": query,
                "since": None,
                "until": None,
                "limit": 50,
            },
        )
        return [str(r["text"]) for r in rows.mappings()]


async def _already_migrated(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT to_regclass('person_opt_out')"))
        return row.scalar() is not None


@pytest_asyncio.fixture
async def optout_schema(clean: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Migration 0008's table and triggers.

    Left alone on a database already at 0008, and created for the test and
    removed afterwards on one that is not -- so this file runs against a
    database at any revision without ever leaving a half-migrated one behind.
    """
    if await _already_migrated(clean):
        async with clean.begin() as conn:
            await conn.execute(text("TRUNCATE person_opt_out"))
        yield clean
        return

    migration = _load_migration()
    async with clean.begin() as conn:
        await conn.execute(text(CREATE_OPT_OUT_TABLE))
        await conn.execute(text(migration.MESSAGE_GUARD))
        await conn.execute(
            text(
                "CREATE TRIGGER trg_message_opt_out "
                "BEFORE INSERT OR UPDATE ON message "
                "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_message()"
            )
        )
        await conn.execute(text(migration.ENTRY_GUARD))
        await conn.execute(
            text(
                "CREATE TRIGGER trg_document_entry_opt_out "
                "BEFORE INSERT OR UPDATE ON document_entry "
                "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_document_entry()"
            )
        )
    try:
        yield clean
    finally:
        async with clean.begin() as conn:
            await conn.execute(
                text("DROP TRIGGER IF EXISTS trg_document_entry_opt_out ON document_entry")
            )
            await conn.execute(
                text("DROP FUNCTION IF EXISTS reject_opted_out_document_entry()")
            )
            await conn.execute(text("DROP TRIGGER IF EXISTS trg_message_opt_out ON message"))
            await conn.execute(text("DROP FUNCTION IF EXISTS reject_opted_out_message()"))
            await conn.execute(text("DROP TABLE IF EXISTS person_opt_out"))


# --- retention ---------------------------------------------------------


async def test_retention_removes_old_content_from_every_path(clean: AsyncEngine) -> None:
    """A message deleted from `message` while its window survives is still
    returned by search, because search reads the window's text and never
    re-derives it from its messages."""
    await seed_channel(clean)
    store = PostgresStore(clean)
    old = msg(1, CUTOFF - timedelta(days=5), content="ancient secret")
    recent = msg(2, NOW - timedelta(days=1), content="current business")
    await store.upsert_messages([old, recent])
    await store.replace_windows(CHANNEL, [window(old)])
    await store.replace_windows(CHANNEL, [window(recent)])

    purged = await PostgresRetentionStore(clean).purge_corpus_before(CUTOFF)

    assert purged.messages == 1
    assert purged.windows == 1
    assert await message_ids(clean) == [2]
    assert await live_window_texts(clean) == ["current business"]
    assert await lexical_hits(clean, "ancient secret") == []
    assert await lexical_hits(clean, "current business") == ["current business"]


async def test_a_window_straddling_the_cutoff_goes_with_its_oldest_content(
    clean: AsyncEngine,
) -> None:
    """Keyed on `starts_at`: a window that begins before the cutoff carries
    pre-cutoff text however recently it ends."""
    await seed_channel(clean)
    store = PostgresStore(clean)
    before = msg(1, CUTOFF - timedelta(hours=1), content="ancient secret")
    after = msg(2, CUTOFF + timedelta(hours=1), content="still current")
    await store.upsert_messages([before, after])
    await store.replace_windows(CHANNEL, [window(before, after)])

    await PostgresRetentionStore(clean).purge_corpus_before(CUTOFF)

    assert await live_window_texts(clean) == []
    # The surviving message stays, to be re-formed into a window of its own.
    assert await message_ids(clean) == [2]
    assert await lexical_hits(clean, "ancient secret") == []


async def test_a_purge_leaves_no_window_without_messages(clean: AsyncEngine) -> None:
    """The cascade drops membership rows and leaves the window: text with
    nothing behind it, still live, still searchable."""
    await seed_channel(clean)
    store = PostgresStore(clean)
    old = msg(1, CUTOFF - timedelta(days=5), content="ancient secret")
    await store.upsert_messages([old])
    await store.replace_windows(CHANNEL, [window(old)])
    # A window whose starts_at is after the cutoff but whose only message is
    # not: the shape the emptiness sweep exists for.
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "UPDATE conversation_window SET starts_at = :t, ends_at = :t"
            ),
            {"t": NOW},
        )

    await PostgresRetentionStore(clean).purge_corpus_before(CUTOFF)

    assert await live_window_texts(clean) == []


async def test_retention_is_re_runnable(clean: AsyncEngine) -> None:
    await seed_channel(clean)
    store = PostgresStore(clean)
    old = msg(1, CUTOFF - timedelta(days=5))
    recent = msg(2, NOW - timedelta(days=1), content="current business")
    await store.upsert_messages([old, recent])
    await store.replace_windows(CHANNEL, [window(old)])
    await store.replace_windows(CHANNEL, [window(recent)])

    retention = PostgresRetentionStore(clean)
    service = RetentionService(retention, RetentionPolicy.from_days(30))
    first = await service.run_once(NOW)
    second = await service.run_once(NOW)

    assert first.total > 0
    assert second.total == 0
    assert await message_ids(clean) == [2]
    assert await live_window_texts(clean) == ["current business"]


async def test_a_purge_schedules_the_survivors_to_be_rewindowed(
    clean: AsyncEngine,
) -> None:
    """Deleting a window takes its surviving neighbours out of retrieval until
    they are re-formed; the watermark is what makes that happen promptly."""
    await seed_channel(clean)
    store = PostgresStore(clean)
    before = msg(1, CUTOFF - timedelta(hours=1))
    after = msg(2, CUTOFF + timedelta(hours=1))
    await store.upsert_messages([before, after])
    await store.replace_windows(CHANNEL, [window(before, after)])

    await PostgresRetentionStore(clean).purge_corpus_before(CUTOFF)

    dirty = await store.dirty_channels()
    assert [c for c, _ in dirty] == [CHANNEL]


# --- opt-out -----------------------------------------------------------


async def test_opting_out_removes_their_messages_and_the_windows_holding_them(
    optout_schema: AsyncEngine,
) -> None:
    engine = optout_schema
    await seed_channel(engine)
    store = PostgresStore(engine)
    hers = msg(1, NOW - timedelta(hours=2), ALICE, "alice said something")
    his = msg(2, NOW - timedelta(hours=1), BOB, "bob replied")
    await store.upsert_messages([hers, his])
    await store.replace_windows(CHANNEL, [window(hers, his)])

    report = await OptOutService(PostgresRetentionStore(engine)).opt_out(ALICE, "asked")

    assert report.corpus.messages == 1
    # The whole window goes: it is one block of text, so her words cannot be
    # taken out of it without taking it out.
    assert await live_window_texts(engine) == []
    assert await message_ids(engine) == [2]
    assert await lexical_hits(engine, "alice said something") == []


async def test_a_backfill_cannot_resurrect_an_opted_out_persons_messages(
    optout_schema: AsyncEngine,
) -> None:
    """The one that decides whether this is an opt-out at all. Discord still
    holds her messages, and backfill re-reads history from Discord."""
    engine = optout_schema
    await seed_channel(engine)
    store = PostgresStore(engine)
    hers = msg(1, NOW - timedelta(hours=2), ALICE, "alice said something")
    await store.upsert_messages([hers])

    await OptOutService(PostgresRetentionStore(engine)).opt_out(ALICE)
    # Exactly what backfill does, through the real write path.
    await store.upsert_messages([hers, msg(3, NOW, ALICE, "and again")])

    assert await message_ids(engine) == []


async def test_opting_back_in_stops_excluding_without_restoring(
    optout_schema: AsyncEngine,
) -> None:
    engine = optout_schema
    await seed_channel(engine)
    store = PostgresStore(engine)
    hers = msg(1, NOW - timedelta(hours=2), ALICE, "alice said something")
    await store.upsert_messages([hers])

    registry = PostgresRetentionStore(engine)
    service = OptOutService(registry)
    await service.opt_out(ALICE)
    await service.opt_in(ALICE)

    assert not await registry.is_opted_out(ALICE)
    # Nothing came back on its own...
    assert await message_ids(engine) == []
    # ...but ingestion works again.
    await store.upsert_messages([hers])
    assert await message_ids(engine) == [1]


async def test_a_withdrawal_is_never_blocked_by_the_guard(
    optout_schema: AsyncEngine,
) -> None:
    """If a purge fails part-way, the rows it left must still be tombstonable."""
    engine = optout_schema
    await seed_channel(engine)
    store = PostgresStore(engine)
    hers = msg(1, NOW - timedelta(hours=2), ALICE, "alice said something")
    await store.upsert_messages([hers])
    # Record the exclusion without purging, which is the half-applied state.
    await PostgresRetentionStore(engine).record_opt_out(ALICE)

    await store.tombstone_message(1, NOW)

    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT deleted_at FROM message WHERE id = 1"))
        assert row.scalar() is not None
    assert await lexical_hits(engine, "alice said something") == []


async def test_an_opted_out_uploader_cannot_re_enter_a_document(
    optout_schema: AsyncEngine,
) -> None:
    """An opt-out that covers messages and leaves the attached file searchable
    has withdrawn the index entry and kept the content."""
    engine = optout_schema
    await seed_channel(engine)
    async with engine.begin() as conn:
        person = await conn.execute(
            text("INSERT INTO person (display_name) VALUES ('alice') RETURNING id")
        )
        person_id = int(person.scalar_one())
        await conn.execute(
            text(
                "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                "VALUES ('discord', :u, :p)"
            ),
            {"u": ALICE.platform_user_id, "p": person_id},
        )
        document = await conn.execute(
            text(
                "INSERT INTO document (identity_key, content_hash, origin, media_type) "
                "VALUES ('k', 'h', 'attachment', 'pdf') RETURNING id"
            )
        )
        document_id = int(document.scalar_one())
        await conn.execute(
            text(
                "INSERT INTO document_entry "
                "(document_id, channel_id, message_id, uploader_person_id, entered_at) "
                "VALUES (:d, :c, 1, :p, now())"
            ),
            {"d": document_id, "c": CH, "p": person_id},
        )

    await PostgresRetentionStore(engine).record_opt_out(ALICE)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_entry "
                "(document_id, channel_id, message_id, uploader_person_id, entered_at) "
                "VALUES (:d, :c, 2, :p, now())"
            ),
            {"d": document_id, "c": CH, "p": person_id},
        )
        rows = await conn.execute(
            text("SELECT message_id FROM document_entry ORDER BY message_id")
        )
        # The pre-existing share is still there for the purge to remove; the
        # re-entry never landed.
        assert [int(r[0]) for r in rows] == [1]
