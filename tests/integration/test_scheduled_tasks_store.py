"""The two statements the scheduled-task design rests on.

`CLAIM_DUE` selects, advances and returns in one statement, and advances to
`now() + interval` rather than `next_run_at + interval`. That second choice is
the whole of "a missed window is skipped, not replayed": an hourly task whose
time passed six times during an outage must send one message when the bot comes
back, not six.

`CREATE_WITHIN_CAP` makes the per-person cap a predicate on the insert rather
than a count read and then checked, because two commands arriving together
would each read the same count and both write.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.schedules_postgres import PostgresScheduleStore
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.schedules import TaskOutcome

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LEO = PersonRef(platform=PLATFORM, platform_user_id=7)
BRUNO = PersonRef(platform=PLATFORM, platform_user_id=9)


async def seed_person(engine: AsyncEngine, person: PersonRef, name: str) -> int:
    async with engine.begin() as conn:
        created = await conn.execute(
            text("INSERT INTO person (display_name) VALUES (:n) RETURNING id"), {"n": name}
        )
        person_id = created.scalar_one()
        await conn.execute(
            text(
                "INSERT INTO person_platform_id "
                "(platform, platform_user_id, person_id) VALUES (:p, :u, :i)"
            ),
            {"p": PLATFORM, "u": person.platform_user_id, "i": person_id},
        )
        return int(person_id)


# --- claiming -----------------------------------------------------------


async def test_a_due_task_is_claimed_once(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresScheduleStore(clean)
    await store.create(LEO, "what did I miss", 1, NOW - timedelta(hours=1))

    first = await store.claim_due(NOW, 10)
    second = await store.claim_due(NOW, 10)

    assert [t.question for t in first] == ["what did I miss"]
    assert second == [], "the claim advanced the schedule, so it is no longer due"


async def test_a_missed_window_runs_once_not_once_per_interval(
    clean: AsyncEngine,
) -> None:
    """The failure this guards: an hourly task whose time passed six times
    during an outage sending six messages the moment the bot returns."""
    await seed_person(clean, LEO, "Leo")
    store = PostgresScheduleStore(clean)
    await store.create(LEO, "hourly", 1, NOW - timedelta(hours=6))

    claimed = await store.claim_due(NOW, 10)
    assert len(claimed) == 1
    assert await store.claim_due(NOW, 10) == []

    # And the next one is an interval from now, not from the missed time.
    tasks = await store.for_person(LEO)
    assert tasks[0].next_run_at == NOW + timedelta(hours=1)


async def test_a_task_not_yet_due_is_not_claimed(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresScheduleStore(clean)
    await store.create(LEO, "later", 6, NOW + timedelta(hours=1))
    assert await store.claim_due(NOW, 10) == []


async def test_a_disabled_task_is_never_claimed(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresScheduleStore(clean)
    await store.create(LEO, "q", 1, NOW - timedelta(hours=1))
    await store.disable(LEO, "direct messages are closed", NOW)

    assert await store.claim_due(NOW, 10) == []


async def test_a_claim_carries_whose_task_it_is(clean: AsyncEngine) -> None:
    """The run is performed as the owner, so the answer is scoped to them."""
    await seed_person(clean, BRUNO, "Bruno")
    store = PostgresScheduleStore(clean)
    await store.create(BRUNO, "q", 1, NOW - timedelta(hours=1))

    claimed = await store.claim_due(NOW, 10)
    assert claimed[0].person == BRUNO


# --- the cap ------------------------------------------------------------


async def test_the_cap_is_enforced_by_the_insert(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    store = PostgresScheduleStore(clean, cap=2)

    assert await store.create(LEO, "one", 6, NOW) is not None
    assert await store.create(LEO, "two", 6, NOW) is not None
    assert await store.create(LEO, "three", 6, NOW) is None


async def test_one_persons_cap_does_not_bind_another(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    await seed_person(clean, BRUNO, "Bruno")
    store = PostgresScheduleStore(clean, cap=1)

    assert await store.create(LEO, "mine", 6, NOW) is not None
    assert await store.create(BRUNO, "theirs", 6, NOW) is not None


async def test_an_unknown_person_creates_nothing(clean: AsyncEngine) -> None:
    """This path must not be able to invent an identity."""
    store = PostgresScheduleStore(clean)
    assert await store.create(LEO, "q", 6, NOW) is None


# --- ownership ----------------------------------------------------------


async def test_a_person_lists_only_their_own(clean: AsyncEngine) -> None:
    await seed_person(clean, LEO, "Leo")
    await seed_person(clean, BRUNO, "Bruno")
    store = PostgresScheduleStore(clean)
    await store.create(LEO, "mine", 6, NOW)
    await store.create(BRUNO, "theirs", 6, NOW)

    assert [t.question for t in await store.for_person(LEO)] == ["mine"]


async def test_deleting_is_bound_by_owner(clean: AsyncEngine) -> None:
    """Without person_id in the WHERE clause this deletes anybody's task by
    number -- and the number is visible in its owner's own listing."""
    await seed_person(clean, LEO, "Leo")
    await seed_person(clean, BRUNO, "Bruno")
    store = PostgresScheduleStore(clean)
    theirs = await store.create(BRUNO, "theirs", 6, NOW)
    assert theirs is not None

    assert await store.delete(LEO, theirs.id) is False
    assert len(await store.for_person(BRUNO)) == 1
    assert await store.delete(BRUNO, theirs.id) is True


# --- outcomes and removal -----------------------------------------------


async def test_a_run_outcome_is_recorded(clean: AsyncEngine) -> None:
    """Silence is by design, so the record is what makes a quiet task legible."""
    await seed_person(clean, LEO, "Leo")
    store = PostgresScheduleStore(clean)
    task = await store.create(LEO, "q", 6, NOW)
    assert task is not None

    await store.record_run(task.id, TaskOutcome.NOTHING, NOW)

    stored = (await store.for_person(LEO))[0]
    assert stored.last_outcome is TaskOutcome.NOTHING
    assert stored.last_run_at == NOW


async def test_removing_a_person_removes_their_tasks(clean: AsyncEngine) -> None:
    """Opt-out and erasure purge facts, memory and notifications; a task that
    outlived them would keep messaging somebody who asked to be gone."""
    person_id = await seed_person(clean, LEO, "Leo")
    store = PostgresScheduleStore(clean)
    await store.create(LEO, "q", 6, NOW)

    async with clean.begin() as conn:
        await conn.execute(
            text("DELETE FROM person_platform_id WHERE person_id = :i"), {"i": person_id}
        )
        await conn.execute(text("DELETE FROM person WHERE id = :i"), {"i": person_id})

    async with clean.connect() as conn:
        rows = await conn.execute(text("SELECT count(*) FROM scheduled_task"))
        assert rows.scalar_one() == 0


async def test_the_interval_bound_is_in_the_schema(clean: AsyncEngine) -> None:
    """A command is one way in; the floor is a cost control and should not
    depend on the only caller today remembering to check."""
    person_id = await seed_person(clean, LEO, "Leo")
    with pytest.raises(Exception, match="ck_scheduled_task_interval"):
        async with clean.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO scheduled_task "
                    "(person_id, question, interval_hours, next_run_at) "
                    "VALUES (:i, 'q', 0, now())"
                ),
                {"i": person_id},
            )
