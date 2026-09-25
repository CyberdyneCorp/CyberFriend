"""Console sign-in records against a live Postgres (migration 0031).

The unit tests run the flow over in-memory stores; these assert the same
contract survives the real schema: a login is consumed once, a session's
bytea and text[] columns round-trip, revoked and idle sessions are not live,
and `config_audit.operator_display` is written while the append-only trigger
still refuses UPDATE and DELETE.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.admin_postgres import PostgresChangeRecord
from chatmemory.adapters.store.admin_session_postgres import (
    PostgresLoginStore,
    PostgresSessionStore,
)
from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.admin.audit import applied
from chatmemory.admin.auth import Actor
from chatmemory.admin.oidc.store import LoginRecord, RefreshedTokens, SessionRecord

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
IDLE = timedelta(hours=12)
PERSON = Actor("oidc:ana-sub", "ana@cyberdyne.test")


@pytest.fixture(autouse=True)
async def _require_the_sign_in_schema(clean: AsyncEngine) -> None:
    """Fail, rather than skip, when 0031 has not been applied."""
    async with clean.connect() as conn:
        found = await conn.scalar(text("SELECT to_regclass('public.admin_session')"))
    assert found is not None, "migration 0031 has not been applied"


def _login(
    state_hash: str = "s1", expires_at: datetime = NOW + timedelta(minutes=10)
) -> LoginRecord:
    return LoginRecord(
        state_hash=state_hash,
        nonce_hash="n1",
        verifier_enc=b"\x00sealed-verifier",
        binding_hash="b1",
        expires_at=expires_at,
    )


def _session(id_hash: str = "h1") -> SessionRecord:
    return SessionRecord(
        id_hash=id_hash,
        sub="ana-sub",
        email="ana@cyberdyne.test",
        roles=("admin", "operator"),
        access_token_enc=b"\x01access",
        refresh_token_enc=b"\x02refresh",
        id_token_enc=b"\x03id",
        access_expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        last_seen_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )


async def test_a_login_is_consumed_exactly_once(clean: AsyncEngine) -> None:
    logins = PostgresLoginStore(clean)
    await logins.begin(_login(), NOW)

    first = await logins.consume("s1", NOW)
    second = await logins.consume("s1", NOW)

    assert first == _login()
    assert second is None


async def test_an_expired_login_is_not_consumed(clean: AsyncEngine) -> None:
    logins = PostgresLoginStore(clean)
    await logins.begin(_login(expires_at=NOW), NOW - timedelta(minutes=10))

    assert await logins.consume("s1", NOW) is None


async def test_old_logins_are_dropped_when_a_new_one_begins(clean: AsyncEngine) -> None:
    logins = PostgresLoginStore(clean)
    await logins.begin(_login("old", expires_at=NOW - timedelta(days=2)), NOW - timedelta(days=3))
    await logins.begin(_login("new"), NOW)

    async with clean.connect() as conn:
        states = set((await conn.execute(text("SELECT state_hash FROM admin_login"))).scalars())
    assert states == {"new"}


async def test_a_session_round_trips(clean: AsyncEngine) -> None:
    sessions = PostgresSessionStore(clean)
    await sessions.create(_session())

    assert await sessions.live("h1", NOW, IDLE) == _session()
    assert await sessions.live("other", NOW, IDLE) is None


async def test_an_idle_or_expired_session_is_not_live(clean: AsyncEngine) -> None:
    sessions = PostgresSessionStore(clean)
    await sessions.create(_session())

    assert await sessions.live("h1", NOW + IDLE + timedelta(seconds=1), IDLE) is None
    await sessions.touch("h1", NOW + timedelta(days=29, hours=23))
    assert await sessions.live("h1", NOW + timedelta(days=30), IDLE) is None


async def test_a_refresh_replaces_the_tokens(clean: AsyncEngine) -> None:
    sessions = PostgresSessionStore(clean)
    await sessions.create(_session())
    later = NOW + timedelta(minutes=14)

    await sessions.refreshed(
        "h1",
        RefreshedTokens(
            roles=("operator",),
            access_token_enc=b"\x11new-access",
            refresh_token_enc=b"\x12new-refresh",
            id_token_enc=b"\x03id",
            access_expires_at=later + timedelta(minutes=15),
            expires_at=later + timedelta(days=30),
        ),
        later,
    )

    found = await sessions.live("h1", later, IDLE)
    assert found is not None
    assert found.roles == ("operator",)
    assert found.refresh_token_enc == b"\x12new-refresh"
    assert found.last_seen_at == later


async def test_a_revoked_session_is_returned_once_and_never_live_again(
    clean: AsyncEngine,
) -> None:
    sessions = PostgresSessionStore(clean)
    await sessions.create(_session())

    revoked = await sessions.revoke("h1", NOW)

    assert revoked is not None and revoked.id_token_enc == b"\x03id"
    assert await sessions.revoke("h1", NOW) is None
    assert await sessions.live("h1", NOW, IDLE) is None


# --- the change record -------------------------------------------------


async def test_a_signed_in_change_is_recorded_with_the_email(clean: AsyncEngine) -> None:
    record = PostgresChangeRecord(clean)

    await record.record(applied(PERSON, "retention_days", "30", "90"))

    [entry] = await record.recent()
    assert (entry.operator, entry.operator_display) == ("oidc:ana-sub", "ana@cyberdyne.test")


async def test_a_setting_stored_after_sign_in_carries_the_email(clean: AsyncEngine) -> None:
    store = PostgresConfigurationStore(clean)

    await store.put("ask_min_confidence", "0.7", PERSON.name, display=PERSON.display)
    await store.record_refusal("discord_token", PERSON.name, "no", display=PERSON.display)
    await store.clear("ask_min_confidence", PERSON.name, display=PERSON.display)

    entries = await PostgresChangeRecord(clean).recent()
    assert [e.operator_display for e in entries] == ["ana@cyberdyne.test"] * 3


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE config_audit SET operator_display = 'someone@else'",
        "UPDATE config_audit SET operator = 'oidc:mallory'",
        "DELETE FROM config_audit",
    ],
)
async def test_the_record_is_still_append_only_after_0031(
    clean: AsyncEngine, statement: str
) -> None:
    record = PostgresChangeRecord(clean)
    await record.record(applied(PERSON, "retention_days", "30", "90"))

    with pytest.raises(SQLAlchemyError) as raised:
        async with clean.begin() as conn:
            await conn.execute(text(statement))
    assert "append-only" in str(raised.value)

    [survivor] = await record.recent()
    assert survivor.operator_display == "ana@cyberdyne.test"
