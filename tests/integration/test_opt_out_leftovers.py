"""What used to survive an opt-out, against the real schema.

Each of these was a store holding a person's data that the `person_opt_out`
triggers did not reach: a scheduled task kept asking questions on their
behalf and messaging them, an MCP token kept authenticating as them, and the
fetch log kept recording which of their messages linked which URL. Migration
0028 folds every per-table purge into one function, `purge_person_derived`,
so opt-out and erasure delete through the same list.

Run against a database at head (`alembic upgrade head`), because what is
under test is the shipped trigger and function.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.adapters.store.schedules_postgres import PostgresScheduleStore
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import PersonRef
from chatmemory.mcp.auth import PostgresTokenStore
from tests.integration.test_alert_kinds_store import alembic

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
ALICE = PersonRef(PLATFORM, 8101)
BOB = PersonRef(PLATFORM, 8102)
CHANNEL = 810


async def _seed_person(engine: AsyncEngine, person: PersonRef, name: str) -> int:
    async with engine.begin() as conn:
        person_id = (
            await conn.execute(
                text("INSERT INTO person (display_name) VALUES (:n) RETURNING id"), {"n": name}
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                "VALUES (:p, :u, :i)"
            ),
            {"p": PLATFORM, "u": person.platform_user_id, "i": person_id},
        )
        return int(person_id)


async def _count(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


async def _seed_message(engine: AsyncEngine, message_id: int, author_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO channel (id, platform, name) VALUES (:c, :p, 'general') "
                "ON CONFLICT DO NOTHING"
            ),
            {"c": CHANNEL, "p": PLATFORM},
        )
        await conn.execute(
            text(
                "INSERT INTO message (id, channel_id, author_person_id, content, created_at) "
                "VALUES (:id, :c, :a, 'see https://example.com', :at)"
            ),
            {"id": message_id, "c": CHANNEL, "a": author_id, "at": NOW},
        )


async def _record_fetch(engine: AsyncEngine, message_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_fetch "
                "(target, outcome, channel_id, message_id, attempts, fetched_at) "
                "VALUES ('https://example.com', 'fetched', :c, :m, 1, :at)"
            ),
            {"c": CHANNEL, "m": message_id, "at": NOW},
        )


def _service(engine: AsyncEngine) -> OptOutService:
    return OptOutService(PostgresRetentionStore(engine))


# --- scheduled tasks ------------------------------------------------------


async def test_opting_out_deletes_the_persons_scheduled_tasks(clean: AsyncEngine) -> None:
    """The bug: the task outlived the opt-out, kept running on a timer and
    kept messaging somebody who had asked to be gone."""
    await _seed_person(clean, ALICE, "Alice")
    await _seed_person(clean, BOB, "Bob")
    schedules = PostgresScheduleStore(clean)
    await schedules.create(ALICE, "what did I miss", 1, NOW - timedelta(hours=1))
    await schedules.create(BOB, "anything new", 1, NOW - timedelta(hours=1))

    await _service(clean).opt_out(ALICE, "asked")

    assert await schedules.for_person(ALICE) == []
    due = await schedules.claim_due(NOW, 10)
    assert [t.person for t in due] == [BOB], "only the person still in runs"


async def test_a_due_task_of_an_opted_out_person_never_runs(clean: AsyncEngine) -> None:
    """Defence in depth: a task written after the opt-out (the table has no
    insert guard) is still never claimed by the sweep."""
    alice_id = await _seed_person(clean, ALICE, "Alice")
    await _service(clean).opt_out(ALICE)
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO scheduled_task (person_id, question, interval_hours, next_run_at) "
                "VALUES (:p, 'late', 1, :at)"
            ),
            {"p": alice_id, "at": NOW - timedelta(hours=1)},
        )

    assert await PostgresScheduleStore(clean).claim_due(NOW, 10) == []


# --- MCP tokens -------------------------------------------------------------


async def test_opting_out_removes_the_persons_mcp_tokens(clean: AsyncEngine) -> None:
    await _seed_person(clean, ALICE, "Alice")
    await _seed_person(clean, BOB, "Bob")
    tokens = PostgresTokenStore(clean)
    hers = await tokens.issue(ALICE, "laptop")
    his = await tokens.issue(BOB, "desktop")

    await _service(clean).opt_out(ALICE)

    assert await tokens.person_for_token(hers.token) is None
    assert await tokens.person_for_token(his.token) == BOB
    assert await _count(
        clean,
        "SELECT count(*) FROM mcp_token WHERE platform_user_id = :u",
        u=ALICE.platform_user_id,
    ) == 0


async def test_a_token_issued_after_the_opt_out_never_authenticates(
    clean: AsyncEngine,
) -> None:
    """The bug: opt-out deleted the tokens, but `issue`/`rotate` had no guard
    and the lookup no opt-out check, so an operator issuing one afterwards
    handed out a credential that authenticated as the opted-out person."""
    await _seed_person(clean, ALICE, "Alice")
    await _seed_person(clean, BOB, "Bob")
    await _service(clean).opt_out(ALICE)
    tokens = PostgresTokenStore(clean)

    issued = await tokens.issue(ALICE, "late")
    rotated = await tokens.rotate(ALICE, "later")
    his = await tokens.issue(BOB, "desktop")

    assert await tokens.person_for_token(issued.token) is None
    assert await tokens.person_for_token(rotated.token) is None
    assert await tokens.person_for_token(his.token) == BOB


async def test_a_token_for_somebody_never_seen_still_authenticates(
    clean: AsyncEngine,
) -> None:
    """The opt-out check joins through `person_platform_id`; an identity with
    no person row yet is not opted out and must not be locked out by it."""
    stranger = PersonRef(PLATFORM, 8199)
    tokens = PostgresTokenStore(clean)
    issued = await tokens.issue(stranger, "")

    assert await tokens.person_for_token(issued.token) == stranger


# --- the fetch log ----------------------------------------------------------


async def test_opting_out_deletes_the_fetch_log_of_their_messages(clean: AsyncEngine) -> None:
    alice_id = await _seed_person(clean, ALICE, "Alice")
    bob_id = await _seed_person(clean, BOB, "Bob")
    await _seed_message(clean, 91, alice_id)
    await _seed_message(clean, 92, bob_id)
    await _record_fetch(clean, 91)
    await _record_fetch(clean, 92)

    report = await _service(clean).opt_out(ALICE)

    assert report.corpus.fetch_records == 1
    assert await _count(clean, "SELECT count(*) FROM document_fetch WHERE message_id = 91") == 0
    assert await _count(clean, "SELECT count(*) FROM document_fetch WHERE message_id = 92") == 1


# --- one delete path --------------------------------------------------------


async def test_every_derived_store_is_purged_by_one_function(clean: AsyncEngine) -> None:
    """The function erasure calls directly, and the only thing the opt-out
    trigger does. Idempotent, so a resumed erasure can call it again."""
    alice_id = await _seed_person(clean, ALICE, "Alice")
    await PostgresScheduleStore(clean).create(ALICE, "q", 1, NOW)
    await PostgresTokenStore(clean).issue(ALICE, "")
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO person_fact (person_id, kind, value) "
                "VALUES (:p, 'preferred_name', 'Ali')"
            ),
            {"p": alice_id},
        )

    async with clean.begin() as conn:
        await conn.execute(text("SELECT purge_person_derived(:p)"), {"p": alice_id})
        await conn.execute(text("SELECT purge_person_derived(:p)"), {"p": alice_id})

    for table, where in (
        ("scheduled_task", "person_id = :p"),
        ("person_fact", "person_id = :p"),
        (
            "mcp_token",
            "platform_user_id = (SELECT platform_user_id FROM person_platform_id "
            "WHERE person_id = :p)",
        ),
    ):
        assert await _count(clean, f"SELECT count(*) FROM {table} WHERE {where}", p=alice_id) == 0


async def test_person_opt_out_has_a_single_purge_trigger(clean: AsyncEngine) -> None:
    """The scattered per-table triggers are gone; a new table extends the
    function, not the trigger list, so opt-out and erasure cannot drift."""
    async with clean.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'person_opt_out'::regclass AND NOT tgisinternal"
            )
        )
        names = sorted(r[0] for r in rows)
    assert names == ["trg_person_opt_out_purges_derived"]


# --- the migration ----------------------------------------------------------


async def _seed_task_and_token(engine: AsyncEngine, person: PersonRef, person_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO scheduled_task (person_id, question, interval_hours, next_run_at) "
                "VALUES (:p, 'q', 1, :at)"
            ),
            {"p": person_id, "at": NOW},
        )
        await conn.execute(
            text(
                "INSERT INTO mcp_token (token_hash, platform, platform_user_id) "
                "VALUES (:h, :p, :u)"
            ),
            {"h": f"hash-{person_id}", "p": PLATFORM, "u": person.platform_user_id},
        )


async def test_upgrading_purges_what_earlier_opt_outs_left_behind(clean: AsyncEngine) -> None:
    """The backfill, seeded at 0027 the way the old purge left things: the
    task and token the triggers missed, and the fetch log of messages the old
    opt-out had already hard-deleted -- rows nothing links to the person any
    more. Everyone else's rows, including the fetch log of a message that was
    only tombstoned by a Discord deletion, stay."""
    await clean.dispose()
    try:
        alembic("downgrade", "0027")
        alice_id = await _seed_person(clean, ALICE, "Alice")
        bob_id = await _seed_person(clean, BOB, "Bob")
        await _seed_task_and_token(clean, ALICE, alice_id)
        await _seed_task_and_token(clean, BOB, bob_id)
        await _seed_message(clean, 77, alice_id)
        await _seed_message(clean, 78, bob_id)
        await _record_fetch(clean, 77)
        await _record_fetch(clean, 78)
        async with clean.begin() as conn:
            await conn.execute(
                text("UPDATE message SET deleted_at = :at WHERE id = 78"), {"at": NOW}
            )
            await conn.execute(
                text("INSERT INTO person_opt_out (person_id) VALUES (:p)"), {"p": alice_id}
            )
            # What the pre-0028 OptOutService did to their messages.
            await conn.execute(
                text("DELETE FROM message WHERE author_person_id = :p"), {"p": alice_id}
            )
        await clean.dispose()

        alembic("upgrade", "0028")

        assert await _count(
            clean, "SELECT count(*) FROM scheduled_task WHERE person_id = :p", p=alice_id
        ) == 0
        assert await _count(
            clean,
            "SELECT count(*) FROM mcp_token WHERE platform_user_id = :u",
            u=ALICE.platform_user_id,
        ) == 0
        assert await _count(clean, "SELECT count(*) FROM scheduled_task") == 1
        assert await _count(clean, "SELECT count(*) FROM mcp_token") == 1
        assert await _count(clean, "SELECT count(*) FROM document_fetch WHERE message_id = 77") == 0
        assert await _count(clean, "SELECT count(*) FROM document_fetch WHERE message_id = 78") == 1
    finally:
        await clean.dispose()
        alembic("upgrade", "head")


LEGACY_TRIGGERS = [
    "trg_person_opt_out_purges_facts",
    "trg_person_opt_out_purges_memory",
    "trg_person_opt_out_purges_notifications",
    "trg_person_opt_out_purges_position_alerts",
]


async def test_downgrading_restores_the_per_table_triggers(clean: AsyncEngine) -> None:
    await clean.dispose()
    try:
        alembic("downgrade", "0027")
        async with clean.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'person_opt_out'::regclass AND NOT tgisinternal"
                )
            )
            names = sorted(r[0] for r in rows)
            gone = await conn.scalar(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'purge_person_derived'")
            )
        assert names == LEGACY_TRIGGERS
        assert gone == 0

        alice_id = await _seed_person(clean, ALICE, "Alice")
        async with clean.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO person_fact (person_id, kind, value) "
                    "VALUES (:p, 'preferred_name', 'Ali')"
                ),
                {"p": alice_id},
            )
            await conn.execute(
                text("INSERT INTO person_opt_out (person_id) VALUES (:p)"), {"p": alice_id}
            )
        assert await _count(clean, "SELECT count(*) FROM person_fact") == 0
    finally:
        await clean.dispose()
        alembic("upgrade", "head")
