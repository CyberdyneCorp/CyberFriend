"""The voice-question ledger against a real database.

The caps are the ceiling on the transcription bill, so each test names a way
one could be exceeded -- per person, across people, across the month boundary,
past an opt-out -- and checks the statement refuses before anything is charged.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.media_postgres import PostgresVoiceLedger
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.voice import Reservation, VoiceLimits
from chatmemory.domain.identity import PersonRef

pytestmark = pytest.mark.asyncio

ANA = PersonRef("discord", 2001)
BEA = PersonRef("discord", 2002)
SEPTEMBER = date(2026, 9, 1)
OCTOBER = date(2026, 10, 1)
LIMITS = VoiceLimits(
    max_seconds=120, max_bytes=1, person_monthly_seconds=100, overall_monthly_seconds=150
)


async def charged(engine: AsyncEngine, month: date) -> int:
    async with engine.connect() as conn:
        total = await conn.scalar(
            text("SELECT COALESCE(SUM(seconds), 0) FROM media_usage WHERE month = :m"),
            {"m": month},
        )
    return int(total)


async def test_a_first_question_creates_the_person_and_charges_them(clean: AsyncEngine) -> None:
    ledger = PostgresVoiceLedger(clean)

    assert await ledger.reserve(ANA, 30, SEPTEMBER, LIMITS) is Reservation.GRANTED
    assert await ledger.reserve(ANA, 30, SEPTEMBER, LIMITS) is Reservation.GRANTED

    assert await charged(clean, SEPTEMBER) == 60


async def test_the_person_cap_refuses_without_charging(clean: AsyncEngine) -> None:
    ledger = PostgresVoiceLedger(clean)
    assert await ledger.reserve(ANA, 90, SEPTEMBER, LIMITS) is Reservation.GRANTED

    assert await ledger.reserve(ANA, 11, SEPTEMBER, LIMITS) is Reservation.PERSON_CAP
    assert await ledger.reserve(ANA, 10, SEPTEMBER, LIMITS) is Reservation.GRANTED
    assert await charged(clean, SEPTEMBER) == 100


async def test_the_monthly_cap_counts_everybody(clean: AsyncEngine) -> None:
    ledger = PostgresVoiceLedger(clean)
    assert await ledger.reserve(ANA, 100, SEPTEMBER, LIMITS) is Reservation.GRANTED

    assert await ledger.reserve(BEA, 51, SEPTEMBER, LIMITS) is Reservation.MONTHLY_CAP
    assert await ledger.reserve(BEA, 50, SEPTEMBER, LIMITS) is Reservation.GRANTED
    assert await charged(clean, SEPTEMBER) == 150


async def test_channel_transcription_counts_for_the_month_but_not_the_person(
    clean: AsyncEngine,
) -> None:
    ledger = PostgresVoiceLedger(clean)
    assert await ledger.reserve(ANA, 1, SEPTEMBER, LIMITS) is Reservation.GRANTED
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO media_usage (person_id, month, purpose, seconds) "
                "SELECT person_id, :m, 'channel', 120 FROM person_platform_id "
                "WHERE platform_user_id = :u"
            ),
            {"m": SEPTEMBER, "u": ANA.platform_user_id},
        )

    # Her own questions: 1 of 100. The month: 121 of 150.
    assert await ledger.reserve(ANA, 30, SEPTEMBER, LIMITS) is Reservation.MONTHLY_CAP
    assert await ledger.reserve(ANA, 29, SEPTEMBER, LIMITS) is Reservation.GRANTED


async def test_a_new_month_starts_from_zero(clean: AsyncEngine) -> None:
    ledger = PostgresVoiceLedger(clean)
    assert await ledger.reserve(ANA, 100, SEPTEMBER, LIMITS) is Reservation.GRANTED

    assert await ledger.reserve(ANA, 100, OCTOBER, LIMITS) is Reservation.GRANTED


async def test_an_opted_out_person_is_never_charged(clean: AsyncEngine) -> None:
    ledger = PostgresVoiceLedger(clean)
    await PostgresRetentionStore(clean).record_opt_out(ANA, "asked")

    assert await ledger.reserve(ANA, 10, SEPTEMBER, LIMITS) is Reservation.OPTED_OUT
    assert await charged(clean, SEPTEMBER) == 0


async def test_deleting_the_person_deletes_their_usage(clean: AsyncEngine) -> None:
    ledger = PostgresVoiceLedger(clean)
    assert await ledger.reserve(ANA, 10, SEPTEMBER, LIMITS) is Reservation.GRANTED
    async with clean.begin() as conn:
        await conn.execute(text("DELETE FROM person_platform_id"))
        await conn.execute(text("DELETE FROM person"))

    assert await charged(clean, SEPTEMBER) == 0


async def test_concurrent_charges_cannot_overrun_a_cap(clean: AsyncEngine) -> None:
    """The check and the charge are one step: twenty notes at once still fit the cap."""
    ledger = PostgresVoiceLedger(clean)
    assert await ledger.reserve(ANA, 1, SEPTEMBER, LIMITS) is Reservation.GRANTED
    tight = VoiceLimits(
        max_seconds=120, max_bytes=1, person_monthly_seconds=61, overall_monthly_seconds=1000
    )

    answers = await asyncio.gather(*(ledger.reserve(ANA, 10, SEPTEMBER, tight) for _ in range(20)))

    assert answers.count(Reservation.GRANTED) == 6
    assert await charged(clean, SEPTEMBER) == 61
