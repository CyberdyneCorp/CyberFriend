"""An opt-out withdraws the traces of what the person asked and wrote.

Before migration 0029 nothing recorded who asked an exported run, so an
opt-out left every trace of the person's own questions in Langfuse, and the
traces quoting their messages went only if a message was deleted in Discord.
Now the opt-out marks both, and queues a Langfuse search by platform id for
traces exported before the asker was recorded.

Run against a database at head (`alembic upgrade head`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.adapters.store.trace_postgres import PostgresTraceIndex
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import PersonRef
from tests.integration.test_alert_kinds_store import alembic
from tests.integration.test_opt_out_leftovers import (
    ALICE,
    BOB,
    _count,
    _seed_message,
    _seed_person,
)

pytestmark = pytest.mark.asyncio

ALICE_SLACK = PersonRef("slack", 8201)


def _service(engine: AsyncEngine) -> OptOutService:
    return OptOutService(PostgresRetentionStore(engine), traces=PostgresTraceIndex(engine))


async def _pending(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT trace_id FROM trace_export "
                "WHERE deletion_requested_at IS NOT NULL AND deleted_at IS NULL"
            )
        )
        return {str(r[0]) for r in rows}


async def _open_searches(engine: AsyncEngine) -> list[int]:
    return list(await PostgresTraceIndex(engine).open_asker_searches(10))


async def _link(engine: AsyncEngine, person: PersonRef, person_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                "VALUES (:p, :u, :i)"
            ),
            {"p": person.platform, "u": person.platform_user_id, "i": person_id},
        )


async def test_opting_out_marks_asked_and_quoting_traces(clean: AsyncEngine) -> None:
    """The bug: the traces of Alice's own questions stayed in Langfuse."""
    alice_id = await _seed_person(clean, ALICE, "Alice")
    bob_id = await _seed_person(clean, BOB, "Bob")
    await _seed_message(clean, 91, alice_id)
    await _seed_message(clean, 92, bob_id)
    index = PostgresTraceIndex(clean)
    await index.record_export("alice-asked", [92], ALICE)
    await index.record_export("bob-quotes-alice", [91], BOB)
    await index.record_export("bob-asked", [92], BOB)
    await index.record_export("web-only", [], None)

    report = await _service(clean).opt_out(ALICE, "asked")

    assert report.traces == 2
    assert await _pending(clean) == {"alice-asked", "bob-quotes-alice"}
    assert await _open_searches(clean) == [ALICE.platform_user_id]


async def test_every_platform_id_of_the_person_is_covered(clean: AsyncEngine) -> None:
    alice_id = await _seed_person(clean, ALICE, "Alice")
    await _link(clean, ALICE_SLACK, alice_id)
    index = PostgresTraceIndex(clean)
    await index.record_export("asked-on-slack", [], ALICE_SLACK)

    await _service(clean).opt_out(ALICE)

    assert await _pending(clean) == {"asked-on-slack"}
    assert sorted(await _open_searches(clean)) == sorted(
        [ALICE.platform_user_id, ALICE_SLACK.platform_user_id]
    )


async def test_a_trace_exported_after_the_opt_out_is_born_pending(clean: AsyncEngine) -> None:
    """A run already answering when the opt-out landed exports afterwards;
    the opt-out's marking has already happened and would miss it."""
    await _seed_person(clean, ALICE, "Alice")
    await _service(clean).opt_out(ALICE)

    index = PostgresTraceIndex(clean)
    await index.record_export("late", [], ALICE)
    await index.record_export("bob", [], BOB)

    assert await _pending(clean) == {"late"}


async def test_a_found_trace_is_recorded_pending_and_the_search_closed(
    clean: AsyncEngine,
) -> None:
    await _seed_person(clean, ALICE, "Alice")
    await _service(clean).opt_out(ALICE)
    index = PostgresTraceIndex(clean)
    started = datetime.now(UTC)

    await index.record_found_traces(ALICE.platform_user_id, ["unindexed"], started)

    assert await _pending(clean) == {"unindexed"}
    assert await _count(
        clean,
        "SELECT count(*) FROM trace_export WHERE trace_id = 'unindexed' "
        "AND asker_platform_user_id = :u",
        u=ALICE.platform_user_id,
    ) == 1
    assert await _open_searches(clean) == []


async def test_a_trace_already_deleted_is_not_reopened(clean: AsyncEngine) -> None:
    """Langfuse deletes asynchronously and may still list a deleted trace."""
    index = PostgresTraceIndex(clean)
    await index.record_export("gone", [], ALICE)
    await index.request_deletion_for_asker([ALICE.platform_user_id])
    await index.confirm_deleted(["gone"])

    await index.record_found_traces(ALICE.platform_user_id, ["gone"], datetime.now(UTC))

    assert await _pending(clean) == set()


async def test_a_search_requested_again_meanwhile_stays_open(clean: AsyncEngine) -> None:
    await _seed_person(clean, ALICE, "Alice")
    index = PostgresTraceIndex(clean)
    started = datetime.now(UTC) - timedelta(minutes=1)
    await _service(clean).opt_out(ALICE)

    await index.record_found_traces(ALICE.platform_user_id, [], started)

    assert await _open_searches(clean) == [ALICE.platform_user_id]


async def test_opting_out_again_reopens_a_finished_search(clean: AsyncEngine) -> None:
    """Between opting back in and out again the person may have been traced."""
    await _seed_person(clean, ALICE, "Alice")
    service = _service(clean)
    index = PostgresTraceIndex(clean)
    await service.opt_out(ALICE)
    await index.record_found_traces(ALICE.platform_user_id, [], datetime.now(UTC))
    await service.opt_in(ALICE)

    await service.opt_out(ALICE)

    assert await _open_searches(clean) == [ALICE.platform_user_id]


async def test_upgrading_queues_a_search_for_earlier_opt_outs(clean: AsyncEngine) -> None:
    """Their traces were exported with no recorded asker; only the search
    reaches them."""
    await clean.dispose()
    try:
        alembic("downgrade", "0028")
        alice_id = await _seed_person(clean, ALICE, "Alice")
        await _seed_person(clean, BOB, "Bob")
        async with clean.begin() as conn:
            await conn.execute(
                text("INSERT INTO person_opt_out (person_id) VALUES (:p)"), {"p": alice_id}
            )
            await conn.execute(
                text("INSERT INTO trace_export (trace_id) VALUES ('legacy')")
            )
        await clean.dispose()

        alembic("upgrade", "0029")

        assert await _open_searches(clean) == [ALICE.platform_user_id]
        assert await _count(
            clean,
            "SELECT count(*) FROM trace_export "
            "WHERE trace_id = 'legacy' AND asker_platform_user_id IS NULL",
        ) == 1
    finally:
        await clean.dispose()
        alembic("upgrade", "head")


async def test_downgrading_drops_the_asker_column_and_queue(clean: AsyncEngine) -> None:
    await clean.dispose()
    try:
        alembic("downgrade", "0028")
        assert await _count(
            clean,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'trace_export' AND column_name = 'asker_platform_user_id'",
        ) == 0
        assert await _count(
            clean, "SELECT count(*) FROM pg_tables WHERE tablename = 'trace_asker_search'"
        ) == 0
    finally:
        await clean.dispose()
        alembic("upgrade", "head")

