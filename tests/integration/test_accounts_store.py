"""Account-provisioning records (0035) against the real schema.

The limits hold under concurrency because they are decided under a lock on
the person row; the email is stored only as an HMAC; link codes are stored
hashed, work once, for 15 minutes, and a newer code ends the older ones; and
opt-out and "delete everything" remove every account row through
`purge_person_derived`.

Run against a database at head (`alembic upgrade head`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.accounts_postgres import PostgresAccountStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.accounts import (
    LINK_CODE_TTL,
    LINK_CODES_PER_DAY,
    AccountRecordsRetention,
    code_digest,
    email_hmac,
)
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.accounts import LinkCodeVerdict, ProvisioningLimits
from tests.integration.test_alert_kinds_store import alembic

PLATFORM = "discord"
NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
LEO = PersonRef(PLATFORM, 9101)
ANA = PersonRef(PLATFORM, 9102)
KEY = b"k" * 32
EMAIL = "leo@example.com"
DIGEST = email_hmac(KEY, EMAIL)
LIMITS = ProvisioningLimits()
ACCOUNT_TABLES = (
    "account_consent",
    "account_provisioning_request",
    "account_link_code",
    "person_account_link",
)


async def _reserve(store: PostgresAccountStore, person: PersonRef, at: datetime) -> bool:
    reservation = await store.reserve(person, DIGEST, 1, now=at, limits=LIMITS)
    return reservation.request_id is not None


async def _person_id(engine: AsyncEngine, person: PersonRef) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT person_id FROM person_platform_id WHERE platform_user_id = :u"),
            {"u": person.platform_user_id},
        )
    return int(found)


async def _count(engine: AsyncEngine, table: str, person_id: int) -> int:
    async with engine.connect() as conn:
        return int(
            await conn.scalar(
                text(f"SELECT count(*) FROM {table} WHERE person_id = :p"), {"p": person_id}
            )
            or 0
        )


# --- limits -----------------------------------------------------------------------


async def test_one_request_a_day_and_three_in_thirty_days(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)

    assert await _reserve(store, LEO, NOW)
    second = await store.reserve(LEO, DIGEST, 1, now=NOW + timedelta(hours=23), limits=LIMITS)
    assert second.request_id is None and second.retry_at == NOW + timedelta(hours=24)
    assert await _reserve(store, LEO, NOW + timedelta(days=2))
    assert await _reserve(store, LEO, NOW + timedelta(days=4))
    fourth = await store.reserve(LEO, DIGEST, 1, now=NOW + timedelta(days=6), limits=LIMITS)
    assert fourth.request_id is None and fourth.retry_at == NOW + timedelta(days=30)
    assert await _reserve(store, ANA, NOW + timedelta(days=6)), "limits are per person"

    leo = await _person_id(clean, LEO)
    assert await _count(clean, "account_provisioning_request", leo) == 3
    assert await _count(clean, "account_consent", leo) == 3


async def test_parallel_presses_are_counted_once(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _reserve(store, LEO, NOW - timedelta(days=5))  # creates the person row first

    results = await asyncio.gather(*(_reserve(store, LEO, NOW) for _ in range(5)))

    assert sorted(results) == [False, False, False, False, True]


async def test_a_released_request_is_not_counted(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    reservation = await store.reserve(LEO, DIGEST, 1, now=NOW, limits=LIMITS)
    assert reservation.request_id is not None

    await store.release(reservation.request_id)

    assert await store.last_requests(LEO, NOW - timedelta(days=30)) == []
    assert await _reserve(store, LEO, NOW + timedelta(minutes=1))


async def test_the_email_is_stored_only_as_its_hmac(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _reserve(store, LEO, NOW)
    await store.issue_link_code(
        LEO, code_digest("c"), now=NOW, ttl=LINK_CODE_TTL, per_day=LINK_CODES_PER_DAY
    )

    async with clean.connect() as conn:
        for table in ACCOUNT_TABLES[:3]:
            dumped = await conn.scalar(text(f"SELECT string_agg(t::text, ' ') FROM {table} t"))
            assert EMAIL not in str(dumped).lower(), table
        consent = await conn.scalar(text("SELECT email_hmac FROM account_consent"))
    assert bytes(consent) == DIGEST


# --- link codes -----------------------------------------------------------------------


async def _issue(store: PostgresAccountStore, code: str, at: datetime) -> LinkCodeVerdict:
    issued = await store.issue_link_code(
        LEO, code_digest(code), now=at, ttl=LINK_CODE_TTL, per_day=LINK_CODES_PER_DAY
    )
    return issued.verdict


async def test_no_code_without_consent(clean: AsyncEngine) -> None:
    assert await _issue(PostgresAccountStore(clean), "c", NOW) is LinkCodeVerdict.NO_CONSENT


async def test_a_code_is_hashed_and_works_once(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _reserve(store, LEO, NOW)
    assert await _issue(store, "secret-code", NOW) is LinkCodeVerdict.ISSUED

    async with clean.connect() as conn:
        stored = await conn.scalar(text("SELECT code_sha256 FROM account_link_code"))
    assert bytes(stored) == code_digest("secret-code")

    digest = code_digest("secret-code")
    redeemed = await store.redeem_link_code(digest, NOW + timedelta(minutes=1))
    assert redeemed is not None
    assert redeemed.person_id == await _person_id(clean, LEO) and redeemed.email_hmac == DIGEST
    assert await store.redeem_link_code(digest, NOW + timedelta(minutes=2)) is None


async def test_a_code_expires_after_fifteen_minutes(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _reserve(store, LEO, NOW)
    await _issue(store, "c", NOW)

    assert await store.redeem_link_code(code_digest("c"), NOW + timedelta(minutes=15)) is None


async def test_a_new_code_ends_the_earlier_one(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _reserve(store, LEO, NOW)
    await _issue(store, "first", NOW)
    await _issue(store, "second", NOW + timedelta(minutes=1))

    at = NOW + timedelta(minutes=2)
    assert await store.redeem_link_code(code_digest("first"), at) is None
    assert await store.redeem_link_code(code_digest("second"), at) is not None


async def test_five_codes_a_day(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _reserve(store, LEO, NOW)
    for minute in range(LINK_CODES_PER_DAY):
        issued = await _issue(store, f"c{minute}", NOW + timedelta(minutes=minute))
        assert issued is LinkCodeVerdict.ISSUED

    sixth = await store.issue_link_code(
        LEO, code_digest("c9"), now=NOW + timedelta(hours=1), ttl=LINK_CODE_TTL, per_day=5
    )

    assert sixth.verdict is LinkCodeVerdict.LIMITED
    assert sixth.retry_at == NOW + timedelta(hours=24)
    assert await _issue(store, "c10", NOW + timedelta(hours=24, seconds=1)) is (
        LinkCodeVerdict.ISSUED
    )


# --- cleanup and purge ---------------------------------------------------------------


async def test_the_cleanup_deletes_what_no_longer_counts(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _reserve(store, LEO, NOW - timedelta(days=31))
    await _issue(store, "old", NOW - timedelta(days=2))
    await _reserve(store, LEO, NOW - timedelta(days=3))
    await _issue(store, "new", NOW - timedelta(hours=1))

    cleaned = await AccountRecordsRetention(store).sweep(NOW)

    assert (cleaned.requests, cleaned.codes) == (1, 1)
    assert len(await store.last_requests(LEO, NOW - timedelta(days=60))) == 1
    leo = await _person_id(clean, LEO)
    assert await _count(clean, "account_consent", leo) == 2, "consent stays until erasure"


async def _seed_everything(engine: AsyncEngine, person: PersonRef, sub: str) -> int:
    store = PostgresAccountStore(engine)
    await _reserve(store, person, NOW)
    await store.issue_link_code(
        person, code_digest(sub), now=NOW, ttl=LINK_CODE_TTL, per_day=LINK_CODES_PER_DAY
    )
    person_id = await _person_id(engine, person)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO person_account_link (person_id, issuer, sub) "
                "VALUES (:p, 'https://auth.example.com', :s)"
            ),
            {"p": person_id, "s": sub},
        )
    return person_id


@pytest.mark.parametrize("path", ["purge", "opt_out"])
async def test_purge_and_opt_out_remove_every_account_row(clean: AsyncEngine, path: str) -> None:
    leo = await _seed_everything(clean, LEO, "sub-leo")
    ana = await _seed_everything(clean, ANA, "sub-ana")

    if path == "purge":
        async with clean.begin() as conn:
            await conn.execute(text("SELECT purge_person_derived(:p)"), {"p": leo})
    else:
        await OptOutService(PostgresRetentionStore(clean)).opt_out(LEO, "console:test")

    for table in ACCOUNT_TABLES:
        assert await _count(clean, table, leo) == 0, table
        assert await _count(clean, table, ana) == 1, f"{table}: someone else's row went"


async def test_downgrading_restores_the_previous_purge(clean: AsyncEngine) -> None:
    await clean.dispose()
    try:
        alembic("downgrade", "0033")
        async with clean.connect() as conn:
            purge = str(
                await conn.scalar(
                    text(
                        "SELECT pg_get_functiondef('purge_person_derived(bigint)'::regprocedure)"
                    )
                )
            )
            left = await conn.scalar(
                text("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'account%'")
            )
        assert "erasure_request" in purge and "account_" not in purge
        assert left == 0
    finally:
        await clean.dispose()
        alembic("upgrade", "head")
