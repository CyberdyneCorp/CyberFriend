"""Linking a CyberdyneAuth subject to a person, and user sessions (0035, 0036).

Against the real schema: a link is made in one transaction with the code's
redemption, only for the consented email's HMAC and an unused, unexpired,
unsuperseded code; one subject belongs to one person; a relink or [Unlink]
ends the subject's user sessions; the bot finds the links nobody has been told
about; and opt-out and "delete everything" remove the user sessions with the
link through `purge_person_derived`.

Run against a database at head (`alembic upgrade head`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.accounts_postgres import PostgresAccountStore
from chatmemory.adapters.store.feature_requests_postgres import PostgresFeatureRequestStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.adapters.store.user_session_postgres import PostgresUserSessionStore
from chatmemory.admin.user.store import RefreshedUserTokens, UserSessionRecord
from chatmemory.app.accounts import (
    LINK_CODE_TTL,
    LINK_CODES_PER_DAY,
    LinkAnnouncements,
    code_digest,
    email_hmac,
)
from chatmemory.app.feature_requests import FeatureRequestService, SubmitOutcome
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.accounts import (
    DiscordProfile,
    LinkNotice,
    LinkOutcome,
    NewLink,
    ProvisioningLimits,
)
from chatmemory.ports.feature_requests import SourceKind, SuggestionSource
from tests.integration.test_alert_kinds_store import alembic

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
LEO = PersonRef("discord", 9201)
ANA = PersonRef("discord", 9202)
KEY = b"k" * 32
ISSUER = "https://auth.example.com"


def _email(person: PersonRef) -> str:
    return f"{person.platform_user_id}@example.com"


async def _consent_and_code(store: PostgresAccountStore, person: PersonRef, code: str) -> None:
    await store.reserve(
        person, email_hmac(KEY, _email(person)), 1, now=NOW, limits=ProvisioningLimits()
    )
    issued = await store.issue_link_code(
        person, code_digest(code), now=NOW, ttl=LINK_CODE_TTL, per_day=LINK_CODES_PER_DAY
    )
    assert issued.retry_at is None


def _link(code: str, email: str, sub: str) -> NewLink:
    return NewLink(
        code_sha256=code_digest(code),
        email_hmac=email_hmac(KEY, email),
        issuer=ISSUER,
        sub=sub,
        email_hint="l***@example.com",
    )


def _session(id_hash: str, sub: str) -> UserSessionRecord:
    return UserSessionRecord(
        id_hash=id_hash,
        sub=sub,
        email=None,
        access_token_enc=b"a",
        refresh_token_enc=b"r",
        id_token_enc=b"i",
        access_expires_at=NOW + timedelta(minutes=15),
        created_at=NOW,
        last_seen_at=NOW,
        expires_at=NOW + timedelta(days=30),
        fresh_auth_at=NOW,
    )


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return await conn.scalar(text(sql), params)


async def _live_sessions(engine: AsyncEngine, sub: str) -> int:
    found = await _scalar(
        engine, "SELECT count(*) FROM user_session WHERE sub = :s AND revoked_at IS NULL", s=sub
    )
    return int(str(found))


# --- linking --------------------------------------------------------------------


async def test_a_code_links_its_person_once(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _consent_and_code(store, LEO, "c1")

    first = await store.link(_link("c1", _email(LEO), "sub-leo"), NOW + timedelta(minutes=1))
    again = await store.link(_link("c1", _email(LEO), "sub-leo"), NOW + timedelta(minutes=2))

    assert (first, again) == (LinkOutcome.LINKED, LinkOutcome.BAD_CODE)
    assert await store.linked_person("sub-leo") == LEO
    assert await store.linked_person("sub-unknown") is None
    hint = await _scalar(clean, "SELECT email_hint FROM person_account_link")
    assert hint == "l***@example.com"


async def test_another_email_links_nothing_and_leaves_the_code_usable(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _consent_and_code(store, LEO, "c1")

    refused = await store.link(_link("c1", "mallory@example.com", "sub-m"), NOW)
    owner = await store.link(_link("c1", _email(LEO).upper(), "sub-leo"), NOW)

    assert refused is LinkOutcome.OTHER_EMAIL
    assert owner is LinkOutcome.LINKED, "the HMAC is of the lowercased address"


@pytest.mark.parametrize("case", ["expired", "superseded", "unknown"])
async def test_an_ended_code_links_nothing(clean: AsyncEngine, case: str) -> None:
    store = PostgresAccountStore(clean)
    await _consent_and_code(store, LEO, "c1")
    at = NOW + timedelta(minutes=1)
    if case == "expired":
        at = NOW + LINK_CODE_TTL + timedelta(seconds=1)
    elif case == "superseded":
        await store.issue_link_code(
            LEO, code_digest("c2"), now=at, ttl=LINK_CODE_TTL, per_day=LINK_CODES_PER_DAY
        )
    code = "nope" if case == "unknown" else "c1"

    outcome = await store.link(_link(code, _email(LEO), "sub-leo"), at)

    assert outcome is LinkOutcome.BAD_CODE
    assert await store.linked_person("sub-leo") is None


async def test_one_subject_is_linked_to_one_person(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _consent_and_code(store, LEO, "c1")
    await store.link(_link("c1", _email(LEO), "shared-sub"), NOW)
    await store.reserve(ANA, email_hmac(KEY, _email(LEO)), 1, now=NOW, limits=ProvisioningLimits())
    await store.issue_link_code(
        ANA, code_digest("c2"), now=NOW, ttl=LINK_CODE_TTL, per_day=LINK_CODES_PER_DAY
    )

    taken = await store.link(_link("c2", _email(LEO), "shared-sub"), NOW)

    assert taken is LinkOutcome.SUBJECT_TAKEN
    assert await store.linked_person("shared-sub") == LEO


async def test_a_relink_ends_the_old_accounts_sessions(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    sessions = PostgresUserSessionStore(clean)
    await _consent_and_code(store, LEO, "c1")
    await store.link(_link("c1", _email(LEO), "old-sub"), NOW)
    await sessions.create(_session("h-old", "old-sub"))
    await store.issue_link_code(
        LEO, code_digest("c2"), now=NOW, ttl=LINK_CODE_TTL, per_day=LINK_CODES_PER_DAY
    )

    relinked = await store.link(_link("c2", _email(LEO), "new-sub"), NOW)

    assert relinked is LinkOutcome.LINKED
    assert await store.linked_person("old-sub") is None
    assert await store.linked_person("new-sub") == LEO
    assert await _live_sessions(clean, "old-sub") == 0


async def test_unlinking_ends_the_subjects_sessions(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    sessions = PostgresUserSessionStore(clean)
    await _consent_and_code(store, LEO, "c1")
    await store.link(_link("c1", _email(LEO), "sub-leo"), NOW)
    await sessions.create(_session("h1", "sub-leo"))

    assert await store.unlink(LEO) is True
    assert await store.unlink(LEO) is False
    assert await store.linked_person("sub-leo") is None
    assert await sessions.live("h1", NOW, timedelta(hours=12)) is None


async def test_each_link_is_announced_once(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _consent_and_code(store, LEO, "c1")
    await _consent_and_code(store, ANA, "c2")
    await store.link(_link("c1", _email(LEO), "sub-leo"), NOW)
    await store.link(_link("c2", _email(ANA), "sub-ana"), NOW + timedelta(seconds=1))
    told: list[LinkNotice] = []

    async def tell(notice: LinkNotice) -> bool:
        told.append(notice)
        return notice.person == LEO  # Ana's DM fails this time

    announcements = LinkAnnouncements(store, clock=lambda: NOW)
    assert await announcements.announce(tell) == 1
    assert [n.person for n in told] == [LEO, ANA]
    assert told[0] == LinkNotice(LEO, "sub-leo", "l***@example.com")

    told.clear()
    assert await announcements.announce(tell) == 0
    assert [n.person for n in told] == [ANA], "only the one not yet told"


async def test_a_relink_to_a_new_account_is_announced_again(clean: AsyncEngine) -> None:
    """The "not you? [Unlink]" DM matters most when the account changes."""
    store = PostgresAccountStore(clean)
    await _consent_and_code(store, LEO, "c1")
    await store.link(_link("c1", _email(LEO), "old-sub"), NOW)
    [first] = await store.unannounced_links(10)
    await store.mark_announced(first, NOW)
    assert await store.unannounced_links(10) == []
    await store.issue_link_code(
        LEO, code_digest("c2"), now=NOW, ttl=LINK_CODE_TTL, per_day=LINK_CODES_PER_DAY
    )

    await store.link(_link("c2", _email(LEO), "new-sub"), NOW + timedelta(minutes=1))

    assert await store.unannounced_links(10) == [LinkNotice(LEO, "new-sub", "l***@example.com")]


async def test_announcing_twice_keeps_the_first_time(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _consent_and_code(store, LEO, "c1")
    await store.link(_link("c1", _email(LEO), "sub-leo"), NOW)
    [notice] = await store.unannounced_links(10)

    await store.mark_announced(notice, NOW)
    await store.mark_announced(notice, NOW + timedelta(hours=1))

    told = await _scalar(clean, "SELECT notified_at FROM person_account_link")
    assert told == NOW


async def test_a_live_code_names_its_holder_without_using_it(clean: AsyncEngine) -> None:
    store = PostgresAccountStore(clean)
    await _consent_and_code(store, LEO, "c1")
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "UPDATE person SET display_name = 'Leo' WHERE id = "
                "(SELECT person_id FROM person_platform_id WHERE platform_user_id = :u)"
            ),
            {"u": LEO.platform_user_id},
        )

    holder = await store.code_holder(code_digest("c1"), NOW)
    expired = await store.code_holder(code_digest("c1"), NOW + LINK_CODE_TTL)
    unknown = await store.code_holder(code_digest("nope"), NOW)

    assert holder == DiscordProfile(LEO, "Leo")
    assert expired is None and unknown is None
    assert await store.link(_link("c1", _email(LEO), "sub-leo"), NOW) is LinkOutcome.LINKED
    assert await store.code_holder(code_digest("c1"), NOW) is None, "a used code names nobody"
    assert await store.linked_profile("sub-leo") == DiscordProfile(LEO, "Leo")
    assert await store.linked_profile("sub-unknown") is None


# --- user sessions ------------------------------------------------------------------


async def test_a_user_session_is_created_refreshed_and_revoked(clean: AsyncEngine) -> None:
    sessions = PostgresUserSessionStore(clean)
    await sessions.create(_session("h1", "sub-leo"))
    idle = timedelta(hours=12)

    live = await sessions.live("h1", NOW, idle)
    assert live is not None and live.fresh_auth_at == NOW and live.sub == "sub-leo"
    refreshed = RefreshedUserTokens(
        access_token_enc=b"a2",
        refresh_token_enc=b"r2",
        id_token_enc=b"i2",
        access_expires_at=NOW + timedelta(minutes=30),
        expires_at=NOW + timedelta(days=30),
    )
    assert await sessions.refreshed("h1", refreshed, NOW + timedelta(minutes=1))
    again = await sessions.live("h1", NOW + timedelta(minutes=1), idle)
    assert again is not None and again.access_token_enc == b"a2"
    assert await sessions.live("h1", NOW + timedelta(hours=13), idle) is None, "idle"

    await sessions.create(_session("h2", "sub-leo"))
    assert await sessions.revoke_subject("sub-leo", NOW) == 2
    assert await sessions.revoke("h1", NOW) is None
    assert await sessions.refreshed("h1", refreshed, NOW) is False


# --- purge -------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["purge", "opt_out"])
async def test_purge_and_opt_out_end_the_persons_user_sessions(
    clean: AsyncEngine, path: str
) -> None:
    store = PostgresAccountStore(clean)
    sessions = PostgresUserSessionStore(clean)
    await _consent_and_code(store, LEO, "c1")
    await _consent_and_code(store, ANA, "c2")
    await store.link(_link("c1", _email(LEO), "sub-leo"), NOW)
    await store.link(_link("c2", _email(ANA), "sub-ana"), NOW)
    await sessions.create(_session("h-leo", "sub-leo"))
    await sessions.create(_session("h-ana", "sub-ana"))
    await sessions.create(_session("h-unlinked", "sub-nobody"))
    leo_id = await _scalar(
        clean,
        "SELECT person_id FROM person_platform_id WHERE platform_user_id = :u",
        u=LEO.platform_user_id,
    )

    if path == "purge":
        async with clean.begin() as conn:
            await conn.execute(text("SELECT purge_person_derived(:p)"), {"p": leo_id})
    else:
        await OptOutService(PostgresRetentionStore(clean)).opt_out(LEO, "console:test")

    remaining = await _scalar(clean, "SELECT array_agg(sub ORDER BY sub) FROM user_session")
    assert remaining == ["sub-ana", "sub-nobody"]
    assert await store.linked_person("sub-leo") is None
    assert await store.linked_person("sub-ana") == ANA


async def test_a_web_suggestion_is_stored_as_web(clean: AsyncEngine) -> None:
    service = FeatureRequestService(PostgresFeatureRequestStore(clean), clock=lambda: NOW)

    result = await service.submit(LEO, "a dark mode", SuggestionSource(SourceKind.WEB, "web"))

    assert result.outcome is SubmitOutcome.RECORDED
    assert await _scalar(clean, "SELECT source_kind FROM feature_request") == "web"


async def test_downgrading_to_0035_restores_its_purge(clean: AsyncEngine) -> None:
    service = FeatureRequestService(PostgresFeatureRequestStore(clean), clock=lambda: NOW)
    await service.submit(LEO, "a dark mode", SuggestionSource(SourceKind.WEB, "web"))
    await clean.dispose()
    try:
        alembic("downgrade", "0035")
        async with clean.connect() as conn:
            purge = str(
                await conn.scalar(
                    text(
                        "SELECT pg_get_functiondef('purge_person_derived(bigint)'::regprocedure)"
                    )
                )
            )
            kind = await conn.scalar(text("SELECT source_kind FROM feature_request"))
            left = await conn.scalar(
                text("SELECT count(*) FROM pg_tables WHERE tablename = 'user_session'")
            )
        assert "person_account_link" in purge and "user_session" not in purge
        assert kind == "command"
        assert left == 0
    finally:
        await clean.dispose()
        alembic("upgrade", "head")
