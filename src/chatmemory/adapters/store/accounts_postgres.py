"""Implements `AccountStore`. The statements live next door."""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import accounts_sql as sql
from chatmemory.adapters.store.feature_requests_postgres import _known_person
from chatmemory.adapters.store.memory_postgres import _person_id
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.accounts import (
    AccountCleanup,
    AccountStore,
    DiscordProfile,
    LinkCodeIssue,
    LinkCodeVerdict,
    LinkNotice,
    LinkOutcome,
    NewLink,
    ProvisioningLimits,
    RedeemedCode,
    Reservation,
    retry_at,
)

CODE_DAY = timedelta(hours=24)
"""The window the per-day link-code limit counts over."""


class PostgresAccountStore:
    """Implements `AccountStore`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def last_requests(self, person: PersonRef, since: datetime) -> Sequence[datetime]:
        async with self._engine.connect() as conn:
            person_id = await _known_person(conn, person)
            if person_id is None:
                return []
            return await _requests_since(conn, person_id, since)

    async def reserve(
        self,
        person: PersonRef,
        email_hmac: bytes,
        consent_version: int,
        *,
        now: datetime,
        limits: ProvisioningLimits,
    ) -> Reservation:
        async with self._engine.begin() as conn:
            person_id = await _locked_person(conn, person)
            earlier = await _requests_since(conn, person_id, now - limits.month)
            wait = retry_at(earlier, now, limits)
            if wait is not None:
                return Reservation(None, retry_at=wait)
            await conn.execute(
                sql.INSERT_CONSENT,
                {
                    "person_id": person_id,
                    "consent_version": consent_version,
                    "email_hmac": email_hmac,
                    "now": now,
                },
            )
            inserted = await conn.execute(
                sql.INSERT_REQUEST,
                {"person_id": person_id, "email_hmac": email_hmac, "now": now},
            )
            return Reservation(int(inserted.scalar_one()))

    async def release(self, request_id: int) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sql.DELETE_REQUEST, {"request_id": request_id})

    async def issue_link_code(
        self,
        person: PersonRef,
        code_sha256: bytes,
        *,
        now: datetime,
        ttl: timedelta,
        per_day: int,
    ) -> LinkCodeIssue:
        async with self._engine.begin() as conn:
            person_id = await _known_person(conn, person)
            if person_id is None:
                return LinkCodeIssue(LinkCodeVerdict.NO_CONSENT)
            await conn.execute(sql.LOCK_PERSON, {"person_id": person_id})
            email_hmac = await conn.scalar(sql.LATEST_CONSENT, {"person_id": person_id})
            if email_hmac is None:
                return LinkCodeIssue(LinkCodeVerdict.NO_CONSENT)
            recent = (
                await conn.execute(
                    sql.CODES_SINCE, {"person_id": person_id, "since": now - CODE_DAY}
                )
            ).one()
            if recent.issued >= per_day:
                return LinkCodeIssue(LinkCodeVerdict.LIMITED, retry_at=recent.oldest + CODE_DAY)
            await conn.execute(sql.SUPERSEDE_CODES, {"person_id": person_id, "now": now})
            await conn.execute(
                sql.INSERT_CODE,
                {
                    "code_sha256": code_sha256,
                    "person_id": person_id,
                    "email_hmac": bytes(email_hmac),
                    "now": now,
                    "expires_at": now + ttl,
                },
            )
            return LinkCodeIssue(LinkCodeVerdict.ISSUED)

    async def redeem_link_code(self, code_sha256: bytes, now: datetime) -> RedeemedCode | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(sql.REDEEM_CODE, {"code_sha256": code_sha256, "now": now})
            ).first()
        if row is None:
            return None
        return RedeemedCode(person_id=int(row.person_id), email_hmac=bytes(row.email_hmac))

    async def cleanup(self, now: datetime, limits: ProvisioningLimits) -> AccountCleanup:
        async with self._engine.begin() as conn:
            requests = await conn.execute(
                sql.DELETE_OLD_REQUESTS, {"before": now - limits.month}
            )
            codes = await conn.execute(sql.DELETE_OLD_CODES, {"before": now - CODE_DAY})
        return AccountCleanup(requests=requests.rowcount, codes=codes.rowcount)

    async def link(self, new: NewLink, now: datetime) -> LinkOutcome:
        try:
            async with self._engine.begin() as conn:
                return await _link(conn, new, now)
        except IntegrityError:
            # The same subject linked by somebody else in between: the unique
            # `sub` refused the second, and nothing of this one was written.
            return LinkOutcome.SUBJECT_TAKEN

    async def linked_person(self, sub: str) -> PersonRef | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(sql.LINKED_PERSON, {"sub": sub})).first()
        return None if row is None else PersonRef(str(row.platform), int(row.platform_user_id))

    async def code_holder(self, code_sha256: bytes, now: datetime) -> DiscordProfile | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(sql.CODE_HOLDER, {"code_sha256": code_sha256, "now": now})
            ).first()
        return None if row is None else _profile(row)

    async def linked_profile(self, sub: str) -> DiscordProfile | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(sql.LINKED_PROFILE, {"sub": sub})).first()
        return None if row is None else _profile(row)

    async def unlink(self, person: PersonRef) -> bool:
        async with self._engine.begin() as conn:
            person_id = await _known_person(conn, person)
            if person_id is None:
                return False
            sub = await conn.scalar(sql.DELETE_LINK, {"person_id": person_id})
            if sub is None:
                return False
            await conn.execute(sql.END_USER_SESSIONS, {"sub": sub})
            return True

    async def unannounced_links(self, limit: int) -> Sequence[LinkNotice]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(sql.UNANNOUNCED_LINKS, {"limit": limit})
            return [
                LinkNotice(
                    person=PersonRef(str(row.platform), int(row.platform_user_id)),
                    sub=str(row.sub),
                    email_hint=str(row.email_hint or ""),
                )
                for row in rows
            ]

    async def mark_announced(self, notice: LinkNotice, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sql.MARK_ANNOUNCED, {"sub": notice.sub, "now": now})


async def _link(conn: AsyncConnection, new: NewLink, now: datetime) -> LinkOutcome:
    code = (
        await conn.execute(
            sql.LOCK_LIVE_CODE, {"code_sha256": new.code_sha256, "now": now}
        )
    ).first()
    if code is None:
        return LinkOutcome.BAD_CODE
    if not hmac.compare_digest(bytes(code.email_hmac), new.email_hmac):
        return LinkOutcome.OTHER_EMAIL
    person_id = int(code.person_id)
    owner = await conn.scalar(sql.SUBJECT_OWNER, {"sub": new.sub})
    if owner is not None and int(owner) != person_id:
        return LinkOutcome.SUBJECT_TAKEN
    previous = await conn.scalar(sql.LINKED_SUBJECT, {"person_id": person_id})
    await conn.execute(
        sql.UPSERT_LINK,
        {
            "person_id": person_id,
            "issuer": new.issuer,
            "sub": new.sub,
            "email_hint": new.email_hint,
            "now": now,
        },
    )
    if previous is not None and previous != new.sub:
        await conn.execute(sql.END_USER_SESSIONS, {"sub": previous})
    await conn.execute(sql.USE_CODE, {"code_sha256": new.code_sha256, "now": now})
    return LinkOutcome.LINKED


def _profile(row: Row[Any]) -> DiscordProfile:
    return DiscordProfile(
        person=PersonRef(str(row.platform), int(row.platform_user_id)),
        display_name=str(row.display_name),
    )


async def _locked_person(conn: AsyncConnection, person: PersonRef) -> int:
    """The person's id, created on miss, locked to the end of the transaction."""
    person_id = await _person_id(conn, person)
    await conn.execute(sql.LOCK_PERSON, {"person_id": person_id})
    return person_id


async def _requests_since(
    conn: AsyncConnection, person_id: int, since: datetime
) -> list[datetime]:
    rows = await conn.execute(sql.REQUESTS_SINCE, {"person_id": person_id, "since": since})
    return [row.requested_at for row in rows]


if TYPE_CHECKING:  # pragma: no cover - exists to fail type-checking, not to run

    def _conforms(store: PostgresAccountStore) -> AccountStore:
        return store
