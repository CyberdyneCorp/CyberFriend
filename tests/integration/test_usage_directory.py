"""Who the usage view must not show, read from the real tables on every request."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.usage_postgres import PostgresUsageDirectory
from chatmemory.app.usage import window_of

pytestmark = pytest.mark.asyncio

TODAY = datetime.now(UTC).date()
WINDOW = window_of(None, None, TODAY)
ERASED_AT = datetime.now(UTC) - timedelta(days=3)
NOTICE_AT = datetime.now(UTC) - timedelta(days=1)


async def _person(engine: AsyncEngine, name: str, *platform_ids: int) -> int:
    async with engine.begin() as conn:
        person_id = int(
            (
                await conn.execute(
                    text("INSERT INTO person (display_name) VALUES (:n) RETURNING id"),
                    {"n": name},
                )
            ).scalar_one()
        )
        for uid in platform_ids:
            await conn.execute(
                text(
                    "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                    "VALUES ('discord', :uid, :pid)"
                ),
                {"uid": uid, "pid": person_id},
            )
    return person_id


async def _run(engine: AsyncEngine, statement: str, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


async def _people(engine: AsyncEngine) -> dict[str, int]:
    ids = {
        "ana": await _person(engine, "Ana", 11),
        "opted": await _person(engine, "Olly", 22, 23),
        "erasing": await _person(engine, "Eve", 33),
        "erased": await _person(engine, "", 44),
    }
    await _run(engine, "INSERT INTO person_opt_out (person_id) VALUES (:p)", p=ids["opted"])
    await _run(
        engine,
        "INSERT INTO erasure_request (person_id, mode) VALUES (:p, 'erase')",
        p=ids["erasing"],
    )
    await _run(
        engine,
        "INSERT INTO erasure_request (person_id, mode, step, completed_at) "
        "VALUES (:p, 'erase', 8, now())",
        p=ids["erased"],
    )
    await _run(
        engine, "UPDATE person SET erased_before = :at WHERE id = :p", at=ERASED_AT,
        p=ids["erased"],
    )
    await _run(
        engine, "UPDATE person SET tracing_notice_at = :at WHERE id = :p", at=NOTICE_AT,
        p=ids["ana"],
    )
    return ids


async def test_opted_out_erasing_and_erased_people_are_excluded(clean: AsyncEngine) -> None:
    await _people(clean)

    exclusions = await PostgresUsageDirectory(clean).exclusions(WINDOW)

    # Every platform id of an opted-out person, and the one being erased.
    assert exclusions.people == frozenset({"22", "23", "33"})
    assert exclusions.erased_before == {"44": ERASED_AT}
    assert not exclusions.excludes_person("11")


async def test_pending_deletions_in_the_window_are_listed(clean: AsyncEngine) -> None:
    now = datetime.now(UTC)
    rows = {
        "pending": (now - timedelta(days=2), now, None),
        "confirmed-just-now": (now - timedelta(days=2), now, now - timedelta(hours=1)),
        "confirmed-long-ago": (now - timedelta(days=9), now, now - timedelta(days=8)),
        "outside-window": (now - timedelta(days=200), now, None),
        "kept": (now - timedelta(days=2), None, None),
    }
    for trace_id, (created, requested, deleted) in rows.items():
        await _run(
            clean,
            "INSERT INTO trace_export (trace_id, created_at, deletion_requested_at, deleted_at) "
            "VALUES (:t, :c, :r, :d)",
            t=trace_id, c=created, r=requested, d=deleted,
        )

    exclusions = await PostgresUsageDirectory(clean).exclusions(WINDOW)

    assert exclusions.trace_ids == frozenset({"pending", "confirmed-just-now"})


async def test_an_opt_out_applies_to_the_next_read(clean: AsyncEngine) -> None:
    ids = await _people(clean)
    directory = PostgresUsageDirectory(clean)
    assert not (await directory.exclusions(WINDOW)).excludes_person("11")

    await _run(clean, "INSERT INTO person_opt_out (person_id) VALUES (:p)", p=ids["ana"])

    assert (await directory.exclusions(WINDOW)).excludes_person("11")


async def test_names_and_the_viewed_person(clean: AsyncEngine) -> None:
    ids = await _people(clean)
    directory = PostgresUsageDirectory(clean)

    names = await directory.names(["11", "44", "99", "not-a-number"])
    ana = await directory.person("11")

    # The erased person's tombstone has no name, so they are shown by id.
    assert names == {"11": "Ana"}
    assert ana is not None
    assert (ana.person_id, ana.display_name, ana.notice_at) == (ids["ana"], "Ana", NOTICE_AT)
    assert await directory.person("99") is None
    assert await directory.person("x") is None


async def test_voice_seconds_leave_out_excluded_people_and_add_the_anonymous_total(
    clean: AsyncEngine,
) -> None:
    ids = await _people(clean)
    month = TODAY.replace(day=1)
    for key, seconds in (("ana", 120), ("opted", 60), ("erasing", 30)):
        await _run(
            clean,
            "INSERT INTO media_usage (person_id, month, purpose, seconds) "
            "VALUES (:p, :m, 'question', :s)",
            p=ids[key], m=month, s=seconds,
        )
    await _run(
        clean,
        "INSERT INTO media_usage_anonymous (month, purpose, seconds) VALUES (:m, 'question', 45)",
        m=month,
    )
    await _run(
        clean,
        "INSERT INTO media_usage_anonymous (month, purpose, seconds) VALUES (:m, 'question', 99)",
        m=date(2000, 1, 1),
    )

    voice = await PostgresUsageDirectory(clean).voice(WINDOW)

    assert voice.by_user == {"11": 120}
    assert voice.anonymous == 45
