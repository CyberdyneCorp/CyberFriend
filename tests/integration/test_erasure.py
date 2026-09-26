"""`/privacy` -> delete everything, against the real schema.

Every personal store is seeded for Alice and for Bob. Erasure, with either
choice, must leave no row of Alice's in any of them and a tombstone person
row (id, platform ids, `erased_before`, and the opt-out flag only with the
second choice); Bob's rows must be untouched. A backfill re-reading Alice's
old messages must not bring them, their mentions, their documents or her name
back; with the first choice a message she sends afterwards is archived. Her
voice seconds move to the anonymous total, so the monthly ceiling does not
move. A crash after the trace step is resumed by the sweep.

Run against a database at head (`alembic upgrade head`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.privacy import kept_section
from chatmemory.adapters.documents.store import PostgresDocumentStore
from chatmemory.adapters.store import media_sql
from chatmemory.adapters.store.erasure_postgres import PostgresErasureStore
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.adapters.store.privacy_postgres import PostgresPrivacyStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.adapters.store.trace_postgres import PostgresTraceIndex
from chatmemory.app.erasure import IDLE_BEFORE_RESUME, ErasureService
from chatmemory.app.language import Language
from chatmemory.app.optout import OptOutService
from chatmemory.app.privacy import RetentionFacts
from chatmemory.app.reasoning.tracing import TraceWithdrawal
from chatmemory.composition import build_erasure
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message
from chatmemory.ports.privacy import (
    ERASED_NAME,
    ErasureCounts,
    ErasureMode,
    ErasureStep,
)
from tests.integration.test_alert_kinds_store import alembic
from tests.integration.test_privacy_inventory import (
    ALICE,
    BOB,
    MONTH,
    PLATFORM,
    READABLE,
    _exec,
    _person,
    _seed_personal,
)

pytestmark = pytest.mark.asyncio

DISCORD_EPOCH_MS = 1420070400000
BEFORE = datetime.now(UTC) - timedelta(days=2)
CHANNEL = ChannelRef(PLATFORM, READABLE)

#: Every table the design promises holds no row of an erased person.
PERSONAL = {
    "message": "SELECT count(*) FROM message WHERE author_person_id = :i",
    "message_media": (
        "SELECT count(*) FROM message_media WHERE message_id = ANY(CAST(:m AS bigint[]))"
    ),
    "media_usage": "SELECT count(*) FROM media_usage WHERE person_id = :i",
    "scheduled_task": "SELECT count(*) FROM scheduled_task WHERE person_id = :i",
    "person_fact": "SELECT count(*) FROM person_fact WHERE person_id = :i",
    "conversation_turn": "SELECT count(*) FROM conversation_turn WHERE person_id = :i",
    "conversation_summary": "SELECT count(*) FROM conversation_summary WHERE person_id = :i",
    "position_alert": "SELECT count(*) FROM position_alert WHERE person_id = :i",
    "feature_request": "SELECT count(*) FROM feature_request WHERE person_id = :i",
    "notification_preference": (
        "SELECT count(*) FROM notification_preference WHERE person_id = :i"
    ),
    "mcp_token": "SELECT count(*) FROM mcp_token WHERE platform_user_id = :u",
    "document_fetch": (
        "SELECT count(*) FROM document_fetch WHERE message_id = ANY(CAST(:m AS bigint[]))"
    ),
    "message_mention": "SELECT count(*) FROM message_mention WHERE person_id = :i",
    "document_entry": "SELECT count(*) FROM document_entry WHERE uploader_person_id = :i",
}


def _snowflake(at: datetime) -> int:
    return (int(at.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


async def _count(engine: AsyncEngine, sql: str, **params: object) -> int:
    return int(str(await _exec(engine, sql, **params)))


async def _message(
    engine: AsyncEngine, message_id: int, author: int, at: datetime, mention: int | None = None
) -> None:
    await _exec(
        engine,
        "INSERT INTO channel (id, platform, name) VALUES (:c, :p, 'c') ON CONFLICT DO NOTHING",
        c=READABLE,
        p=PLATFORM,
    )
    await _exec(
        engine,
        "INSERT INTO message (id, channel_id, author_person_id, content, created_at) "
        "VALUES (:id, :c, :a, 'see https://example.com', :at)",
        id=message_id,
        c=READABLE,
        a=author,
        at=at,
    )
    if mention is not None:
        await _exec(
            engine,
            "INSERT INTO message_mention (message_id, person_id) VALUES (:m, :p)",
            m=message_id,
            p=mention,
        )


async def _media(engine: AsyncEngine, message_id: int, kind: str, transcript: str) -> None:
    await _exec(
        engine,
        "INSERT INTO message_media (message_id, attachment_id, kind, declared_type, byte_size, "
        "source_url, text, status) VALUES (:m, :a, :k, 'x/y', 10, 'https://cdn.example/x', "
        ":t, 'done')",
        m=message_id,
        a=message_id + 1,
        k=kind,
        t=transcript,
    )


async def _document(engine: AsyncEngine, message_id: int, uploader: int) -> None:
    document_id = await _exec(
        engine,
        "INSERT INTO document (identity_key, content_hash, origin, media_type) "
        "VALUES (:k, 'h', 'attachment', 'pdf') "
        "ON CONFLICT (identity_key) DO UPDATE SET content_hash = 'h' RETURNING id",
        k=f"doc-{message_id}",
    )
    await _exec(
        engine,
        "INSERT INTO document_entry "
        "(document_id, channel_id, message_id, uploader_person_id, entered_at) "
        "VALUES (:d, :c, :m, :p, now())",
        d=document_id,
        c=READABLE,
        m=message_id,
        p=uploader,
    )


class Seeded:
    """Alice and Bob, each with a row in every store; Alice's old message ids."""

    def __init__(self, alice: int, bob: int, alice_messages: list[int], bob_message: int) -> None:
        self.alice = alice
        self.bob = bob
        self.alice_messages = alice_messages
        self.bob_message = bob_message


async def _seed(engine: AsyncEngine) -> Seeded:
    alice = await _person(engine, ALICE, "Alice Real Name")
    bob = await _person(engine, BOB, "Bob")
    await _seed_personal(engine, alice, ALICE.platform_user_id)
    await _seed_personal(engine, bob, BOB.platform_user_id)
    await _exec(
        engine,
        "UPDATE person SET tracing_notice_version = 1, tracing_notice_at = now() "
        "WHERE id IN (:a, :b)",
        a=alice,
        b=bob,
    )
    first, second = _snowflake(BEFORE), _snowflake(BEFORE + timedelta(seconds=1))
    bob_message = _snowflake(BEFORE + timedelta(seconds=2))
    await _message(engine, first, alice, BEFORE)
    await _message(engine, second, alice, BEFORE + timedelta(seconds=1))
    # Bob's message mentions Alice: it stays, the mention index entry goes.
    await _message(engine, bob_message, bob, BEFORE + timedelta(seconds=2), mention=alice)
    await _media(engine, first, "voice", "alice said this")
    await _media(engine, bob_message, "image", "bob's cat")
    await _document(engine, first, alice)
    for message_id in (first, bob_message):
        await _exec(
            engine,
            "INSERT INTO document_fetch "
            "(target, outcome, channel_id, message_id, attempts, fetched_at) "
            "VALUES ('https://example.com', 'fetched', :c, :m, 1, now())",
            c=READABLE,
            m=message_id,
        )
    # A trace quoting Alice's message, and one quoting Bob's.
    for trace, message_id in (("quotes-alice", first), ("quotes-bob", bob_message)):
        await _exec(
            engine,
            "INSERT INTO trace_export (trace_id, created_at) VALUES (:t, now())",
            t=trace,
        )
        await _exec(
            engine,
            "INSERT INTO trace_export_message (trace_id, platform_message_id) VALUES (:t, :m)",
            t=trace,
            m=message_id,
        )
    return Seeded(alice, bob, [first, second], bob_message)


async def _left(engine: AsyncEngine, person_id: int, user: int, messages: list[int]) -> dict[
    str, int
]:
    return {
        table: await _count(engine, sql, i=person_id, u=user, m=messages)
        for table, sql in PERSONAL.items()
    }


async def _pending(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT trace_id FROM trace_export "
                "WHERE deletion_requested_at IS NOT NULL AND deleted_at IS NULL"
            )
        )
        return {str(r[0]) for r in rows}


async def _erase(engine: AsyncEngine, mode: ErasureMode) -> None:
    request = await build_erasure(engine).erase(ALICE, mode, ErasureCounts(messages=2))
    assert request.complete and request.step is ErasureStep.COMPLETE
    assert request.counts.messages == 2
    assert request.counts.traces == 2, "the asked trace and the quoting one"


@pytest.mark.parametrize("mode", list(ErasureMode))
async def test_erasure_leaves_a_tombstone_and_nothing_else(
    clean: AsyncEngine, mode: ErasureMode
) -> None:
    seeded = await _seed(clean)
    bob_before = await _left(clean, seeded.bob, BOB.platform_user_id, [seeded.bob_message])

    await _erase(clean, mode)

    left = await _left(clean, seeded.alice, ALICE.platform_user_id, seeded.alice_messages)
    assert left == dict.fromkeys(PERSONAL, 0), {k: v for k, v in left.items() if v}
    assert await _left(
        clean, seeded.bob, BOB.platform_user_id, [seeded.bob_message]
    ) == {**bob_before, "message_mention": 0}, "Bob's rows are untouched"
    assert await _count(
        clean, "SELECT count(*) FROM message WHERE id = :m", m=seeded.bob_message
    ) == 1, "the message that mentioned Alice is Bob's and stays"

    async with clean.connect() as conn:
        person = (
            await conn.execute(
                text(
                    "SELECT display_name, erased_before, tracing_notice_version, "
                    "tracing_notice_at FROM person WHERE id = :i"
                ),
                {"i": seeded.alice},
            )
        ).one()
        platform_ids = (
            await conn.execute(
                text("SELECT platform_user_id FROM person_platform_id WHERE person_id = :i"),
                {"i": seeded.alice},
            )
        ).scalars().all()
        opted_out = await conn.scalar(
            text("SELECT count(*) FROM person_opt_out WHERE person_id = :i"), {"i": seeded.alice}
        )
    assert person.display_name == ERASED_NAME
    assert person.erased_before is not None
    # Not in the kept list, so not kept: asking again brings the notice again.
    assert person.tracing_notice_version is None
    assert person.tracing_notice_at is None
    assert await _count(
        clean,
        "SELECT count(*) FROM person WHERE id = :i AND tracing_notice_version = 1",
        i=seeded.bob,
    ) == 1, "Bob's notice record is untouched"
    assert list(platform_ids) == [ALICE.platform_user_id]
    assert opted_out == (1 if mode is ErasureMode.ERASE_AND_OPT_OUT else 0)
    assert {"quotes-alice", f"t1-{ALICE.platform_user_id}"} <= await _pending(clean)
    assert "quotes-bob" not in await _pending(clean)
    assert f"t1-{BOB.platform_user_id}" not in await _pending(clean)
    assert await PostgresTraceIndex(clean).open_asker_searches(10) == [ALICE.platform_user_id]


#: What records that a person asked or was quoted in a trace. Kept only while a
#: deletion is pending, since it is how the sweep finds the traces.
TRACE_RECORDS = {
    "trace_export": "SELECT count(*) FROM trace_export WHERE asker_platform_user_id = :u",
    "trace_export_message": (
        "SELECT count(*) FROM trace_export_message "
        "WHERE platform_message_id = ANY(CAST(:m AS bigint[]))"
    ),
    "trace_asker_search": "SELECT count(*) FROM trace_asker_search WHERE platform_user_id = :u",
}


class Langfuse:
    """Deletes whatever it is asked to, and finds nothing more by asker."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_traces(self, trace_ids: Sequence[str]) -> bool:
        self.deleted.extend(trace_ids)
        return True

    async def find_traces_by_user(self, platform_user_id: int) -> list[str]:
        return []


@pytest.mark.parametrize("mode", list(ErasureMode))
async def test_once_langfuse_confirms_no_record_of_their_traces_is_left(
    clean: AsyncEngine, mode: ErasureMode
) -> None:
    """After the withdrawal sweep, nothing says when they asked or which
    trace quoted them: a confirmed trace keeps its id and deletion time only."""
    seeded = await _seed(clean)
    # Confirmed before this revision, with the asker still on it: the
    # migration scrubs such rows (`test_the_upgrade_scrubs_confirmed_traces`).
    await _exec(clean, "DELETE FROM trace_export WHERE trace_id LIKE 't2-%'")
    await _erase(clean, mode)
    records = {"u": ALICE.platform_user_id, "m": seeded.alice_messages}

    async def records_left() -> dict[str, int]:
        return {t: await _count(clean, q, **records) for t, q in TRACE_RECORDS.items()}

    assert await records_left() == dict.fromkeys(TRACE_RECORDS, 1), (
        "kept while the deletion is pending: it is how the sweep finds them"
    )

    langfuse = Langfuse()
    index = PostgresTraceIndex(clean)
    withdrawal = TraceWithdrawal(index, langfuse, langfuse)
    await withdrawal.search_askers()
    await withdrawal.drain_pending()

    assert {"quotes-alice", f"t1-{ALICE.platform_user_id}"} <= set(langfuse.deleted)
    assert await records_left() == dict.fromkeys(TRACE_RECORDS, 0)
    assert await _count(
        clean,
        "SELECT count(*) FROM trace_export WHERE trace_id = 'quotes-alice' "
        "AND deleted_at IS NOT NULL",
    ) == 1, "the id stays, so a lagging Langfuse listing does not queue it again"
    assert await _count(
        clean, "SELECT count(*) FROM trace_export_message WHERE trace_id = 'quotes-bob'"
    ) == 1, "Bob's trace is not touched"


async def test_the_upgrade_scrubs_confirmed_traces(clean: AsyncEngine) -> None:
    await clean.dispose()
    try:
        alembic("downgrade", "0031")
        for sql in (
            "INSERT INTO trace_export (trace_id, created_at, asker_platform_user_id, deleted_at) "
            "VALUES ('gone', now(), 9101, now()), ('live', now(), 9101, NULL)",
            "INSERT INTO trace_export_message (trace_id, platform_message_id) "
            "VALUES ('gone', 1), ('live', 2)",
            "INSERT INTO trace_asker_search (platform_user_id, completed_at) "
            "VALUES (9101, now()), (9102, NULL)",
        ):
            await _exec(clean, sql)
        await clean.dispose()
        alembic("upgrade", "head")
        async with clean.connect() as conn:
            rows = await conn.execute(
                text("SELECT trace_id, asker_platform_user_id FROM trace_export")
            )
            askers = {str(r[0]): r[1] for r in rows}
            links = await conn.scalars(text("SELECT trace_id FROM trace_export_message"))
            searches = await conn.scalars(text("SELECT platform_user_id FROM trace_asker_search"))
        assert askers == {"gone": None, "live": 9101}
        assert list(links) == ["live"]
        assert list(searches) == [9102], "a pending search is still work to do"
    finally:
        await clean.dispose()
        alembic("upgrade", "head")


async def test_the_monthly_ceiling_is_unchanged_after_the_fold(clean: AsyncEngine) -> None:
    seeded = await _seed(clean)

    async def everyone(month: date) -> int:
        async with clean.connect() as conn:
            row = (
                await conn.execute(
                    media_sql.MONTH_USAGE, {"person_id": seeded.bob, "month": month}
                )
            ).one()
        return int(row.everyone)

    before = (await everyone(MONTH), await everyone(date(2026, 8, 1)))

    await _erase(clean, ErasureMode.ERASE)

    assert (await everyone(MONTH), await everyone(date(2026, 8, 1))) == before
    assert await _count(clean, "SELECT sum(seconds) FROM media_usage_anonymous") == 590
    assert await _count(
        clean, "SELECT count(*) FROM media_usage_anonymous WHERE month = :m", m=MONTH
    ) == 2, "per month and purpose, with no person"


def _backfilled(message_id: int, at: datetime, mentions: frozenset[PersonRef] = frozenset()) -> (
    Message
):
    return Message(
        platform_message_id=message_id,
        channel=CHANNEL,
        author=ALICE,
        content="re-read from Discord",
        created_at=at,
        mentions=mentions,
        author_display="Alice Real Name",
    )


@pytest.mark.parametrize("mode", list(ErasureMode))
async def test_a_backfill_does_not_reimport_what_was_erased(
    clean: AsyncEngine, mode: ErasureMode
) -> None:
    seeded = await _seed(clean)
    await _erase(clean, mode)
    store = PostgresStore(clean)

    await store.upsert_messages([_backfilled(m, BEFORE) for m in seeded.alice_messages])
    bob_again = Message(
        platform_message_id=seeded.bob_message,
        channel=CHANNEL,
        author=BOB,
        content="re-read, mentioning Alice",
        created_at=BEFORE + timedelta(seconds=2),
        mentions=frozenset({ALICE}),
        author_display="Bob",
    )
    await store.upsert_messages([bob_again])
    await _document(clean, seeded.alice_messages[0], seeded.alice)

    left = await _left(clean, seeded.alice, ALICE.platform_user_id, seeded.alice_messages)
    assert left == dict.fromkeys(PERSONAL, 0), {k: v for k, v in left.items() if v}
    assert await _exec(
        clean, "SELECT display_name FROM person WHERE id = :i", i=seeded.alice
    ) == ERASED_NAME, "an old message does not write her name back"


async def test_after_erasing_and_staying_a_new_message_is_archived(clean: AsyncEngine) -> None:
    seeded = await _seed(clean)
    await _erase(clean, ErasureMode.ERASE)
    later = datetime.now(UTC) + timedelta(minutes=1)
    new_id = _snowflake(later)

    await PostgresStore(clean).upsert_messages([_backfilled(new_id, later)])

    assert await _count(
        clean, "SELECT count(*) FROM message WHERE author_person_id = :i", i=seeded.alice
    ) == 1
    assert await _exec(
        clean, "SELECT display_name FROM person WHERE id = :i", i=seeded.alice
    ) == "Alice Real Name", "a message she sends now carries her name, as anyone's does"


async def test_after_erasing_and_leaving_a_new_message_is_not_archived(
    clean: AsyncEngine,
) -> None:
    seeded = await _seed(clean)
    await _erase(clean, ErasureMode.ERASE_AND_OPT_OUT)
    later = datetime.now(UTC) + timedelta(minutes=1)

    await PostgresStore(clean).upsert_messages([_backfilled(_snowflake(later), later)])

    assert await _count(
        clean, "SELECT count(*) FROM message WHERE author_person_id = :i", i=seeded.alice
    ) == 0
    assert await _exec(
        clean, "SELECT display_name FROM person WHERE id = :i", i=seeded.alice
    ) == ERASED_NAME


class CrashingOptOuts(OptOutService):
    """Dies in step 5 the first time: steps 1-4 have finished."""

    crashed = False

    async def purge_contributions(self, person: PersonRef) -> tuple[object, int]:  # type: ignore[override]
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("process killed")
        return await super().purge_contributions(person)


async def test_a_crash_after_step_4_is_finished_by_the_sweep(clean: AsyncEngine) -> None:
    seeded = await _seed(clean)
    store = PostgresErasureStore(clean)
    optouts = CrashingOptOuts(
        PostgresRetentionStore(clean), PostgresDocumentStore(clean), PostgresTraceIndex(clean)
    )
    with pytest.raises(RuntimeError):
        await ErasureService(store, optouts).erase(ALICE, ErasureMode.ERASE, ErasureCounts())

    [stalled] = await store.open_requests(datetime.now(UTC) + timedelta(seconds=1), 10)
    assert stalled.step is ErasureStep.TRACES_MARKED
    assert stalled.counts.traces == 2
    assert await _count(
        clean, "SELECT count(*) FROM message WHERE author_person_id = :i", i=seeded.alice
    ) == 2, "nothing purged yet"

    soon = ErasureService(store, optouts, clock=lambda: datetime.now(UTC))
    assert await soon.resume_stalled() == 0, "a request just advanced is left to its owner"
    later = ErasureService(
        store, optouts, clock=lambda: datetime.now(UTC) + IDLE_BEFORE_RESUME * 2
    )
    assert await later.resume_stalled() == 1

    left = await _left(clean, seeded.alice, ALICE.platform_user_id, seeded.alice_messages)
    assert left == dict.fromkeys(PERSONAL, 0), {k: v for k, v in left.items() if v}
    assert await store.open_requests(datetime.now(UTC) + timedelta(days=1), 10) == []


async def test_asking_again_returns_the_open_request_and_can_upgrade_it(
    clean: AsyncEngine,
) -> None:
    await _seed(clean)
    store = PostgresErasureStore(clean)

    first = await store.open(ALICE, ErasureMode.ERASE, ErasureCounts(messages=2))
    await store.advance(first, ErasureStep.PURGED)
    again = await store.open(ALICE, ErasureMode.ERASE, ErasureCounts(messages=0))
    upgraded = await store.open(ALICE, ErasureMode.ERASE_AND_OPT_OUT, ErasureCounts())

    assert again.id == first.id and again.step is ErasureStep.PURGED
    assert again.counts.messages == 2, "the counts of the first confirmation stay"
    assert upgraded.id == first.id and upgraded.mode is ErasureMode.ERASE_AND_OPT_OUT
    assert upgraded.step is ErasureStep.RECORDED, "the opt-out step runs again"


async def test_after_erasure_the_kept_list_matches_the_rows_that_remain(
    clean: AsyncEngine,
) -> None:
    """The kept list promises a cleared name and preferences and an anonymous
    voice total; after an erasure that is what the database holds."""
    seeded = await _seed(clean)
    await _erase(clean, ErasureMode.ERASE)

    held = await PostgresPrivacyStore(clean).inventory(ALICE, [READABLE], MONTH)
    kept = kept_section(RetentionFacts(True, 90, 30, None), Language.ENGLISH).description(
        Language.ENGLISH
    )

    assert await _exec(
        clean, "SELECT display_name FROM person WHERE id = :i", i=seeded.alice
    ) == ERASED_NAME
    assert held.notifications.enabled is None, "the preference is back to the default"
    assert held.voice_seconds_this_month == 0 and held.facts == () and held.archiving
    assert "Your name and preferences are cleared" in kept
    assert "An anonymous total of voice minutes" in kept


async def test_downgrading_restores_the_previous_purge_and_guards(clean: AsyncEngine) -> None:
    await clean.dispose()
    try:
        alembic("downgrade", "0031")
        async with clean.connect() as conn:
            purge = await conn.scalar(
                text("SELECT pg_get_functiondef('purge_person_derived(bigint)'::regprocedure)")
            )
            guard = await conn.scalar(
                text("SELECT pg_get_functiondef('reject_opted_out_message()'::regprocedure)")
            )
            column = await conn.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'person' AND column_name = 'erased_before'"
                )
            )
        assert "feature_request" in str(purge) and "erasure_request" not in str(purge)
        assert "erased_before" not in str(guard) and "person_opt_out" in str(guard)
        assert column == 0
    finally:
        await clean.dispose()
        alembic("upgrade", "head")
