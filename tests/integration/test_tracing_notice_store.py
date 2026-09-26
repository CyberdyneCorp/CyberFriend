"""The tracing notice claim against the real `person` row.

Granted once per version, again once for a newer version, never to an
opted-out person, once when two replies race, and for a person nothing has
seen yet.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.tracing_notice_postgres import PostgresTracingNoticeStore
from chatmemory.domain.identity import PersonRef

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
BEA = PersonRef("discord", 4242)


async def _recorded(engine: AsyncEngine, person: PersonRef) -> tuple[int | None, datetime | None]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT p.tracing_notice_version, p.tracing_notice_at FROM person p "
                    "JOIN person_platform_id i ON i.person_id = p.id "
                    "WHERE i.platform = :platform AND i.platform_user_id = :uid"
                ),
                {"platform": person.platform, "uid": person.platform_user_id},
            )
        ).one()
    return row[0], row[1]


async def test_granted_once_per_version_and_recorded(clean: AsyncEngine) -> None:
    store = PostgresTracingNoticeStore(clean)

    assert await store.claim_notice(BEA, 1, NOW)
    assert not await store.claim_notice(BEA, 1, NOW + timedelta(hours=1))
    assert await _recorded(clean, BEA) == (1, NOW)

    later = NOW + timedelta(days=2)
    assert await store.claim_notice(BEA, 2, later)
    assert not await store.claim_notice(BEA, 2, later)
    # An older version never takes a person back.
    assert not await store.claim_notice(BEA, 1, later)
    assert await _recorded(clean, BEA) == (2, later)


async def test_an_opted_out_person_is_never_granted_it(clean: AsyncEngine) -> None:
    store = PostgresTracingNoticeStore(clean)
    async with clean.begin() as conn:
        person_id = (
            await conn.execute(
                text("INSERT INTO person (display_name) VALUES ('Bea') RETURNING id")
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                "VALUES (:platform, :uid, :pid)"
            ),
            {"platform": BEA.platform, "uid": BEA.platform_user_id, "pid": person_id},
        )
        await conn.execute(
            text("INSERT INTO person_opt_out (person_id) VALUES (:p)"), {"p": person_id}
        )

    assert not await store.claim_notice(BEA, 1, NOW)
    assert await _recorded(clean, BEA) == (None, None)


async def test_two_racing_replies_show_it_once(clean: AsyncEngine) -> None:
    store = PostgresTracingNoticeStore(clean)
    # The person exists first, so the race is on the claim, not on creation.
    assert await store.claim_notice(BEA, 1, NOW)

    results = await asyncio.gather(*(store.claim_notice(BEA, 2, NOW) for _ in range(5)))

    assert sorted(results) == [False, False, False, False, True]


async def test_a_person_nothing_has_seen_is_created(clean: AsyncEngine) -> None:
    newcomer = PersonRef("discord", 777)
    assert await PostgresTracingNoticeStore(clean).claim_notice(newcomer, 1, NOW)
    assert await _recorded(clean, newcomer) == (1, NOW)
