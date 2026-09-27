"""The `user_session` record (0036), its port, and an in-memory store for tests.

The same shape as `admin_session` without roles -- a plain user has none that
matter here -- and with `fresh_auth_at`, the verified `auth_time` of the last
sign-in made with `max_age`. Hashes and ciphertext only: the id as sha256, the
tokens as AES-GCM under the session key, sealed by the service.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UserSessionRecord:
    """The server half of a `__Host-cf_user` cookie."""

    id_hash: str
    sub: str
    email: str | None
    access_token_enc: bytes
    refresh_token_enc: bytes | None
    id_token_enc: bytes
    access_expires_at: datetime
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    fresh_auth_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RefreshedUserTokens:
    access_token_enc: bytes
    refresh_token_enc: bytes | None
    id_token_enc: bytes
    access_expires_at: datetime
    expires_at: datetime


class UserSessionStore(Protocol):
    async def create(self, session: UserSessionRecord) -> None: ...

    async def live(self, id_hash: str, now: datetime, idle: timedelta) -> UserSessionRecord | None:
        """The session, if not revoked, not expired and seen within `idle`."""
        ...

    async def refreshed(self, id_hash: str, tokens: RefreshedUserTokens, now: datetime) -> bool:
        """Replace the tokens after a refresh; False if revoked meanwhile."""
        ...

    async def touch(self, id_hash: str, now: datetime) -> None: ...

    async def revoke(self, id_hash: str, now: datetime) -> UserSessionRecord | None:
        """Revoke a live session, returning it, or None."""
        ...

    async def revoke_subject(self, sub: str, now: datetime) -> int:
        """Revoke every live session of one account. Returns how many."""
        ...


class InMemoryUserSessionStore:
    def __init__(self) -> None:
        self.records: dict[str, UserSessionRecord] = {}

    async def create(self, session: UserSessionRecord) -> None:
        self.records[session.id_hash] = session

    async def live(self, id_hash: str, now: datetime, idle: timedelta) -> UserSessionRecord | None:
        found = self.records.get(id_hash)
        if found is None or found.revoked_at is not None or found.expires_at <= now:
            return None
        return found if found.last_seen_at > now - idle else None

    async def refreshed(self, id_hash: str, tokens: RefreshedUserTokens, now: datetime) -> bool:
        found = self.records.get(id_hash)
        if found is None or found.revoked_at is not None:
            return False
        self.records[id_hash] = replace(
            found,
            access_token_enc=tokens.access_token_enc,
            refresh_token_enc=tokens.refresh_token_enc,
            id_token_enc=tokens.id_token_enc,
            access_expires_at=tokens.access_expires_at,
            expires_at=tokens.expires_at,
            last_seen_at=now,
        )
        return True

    async def touch(self, id_hash: str, now: datetime) -> None:
        found = self.records.get(id_hash)
        if found is not None:
            self.records[id_hash] = replace(found, last_seen_at=now)

    async def revoke(self, id_hash: str, now: datetime) -> UserSessionRecord | None:
        found = self.records.get(id_hash)
        if found is None or found.revoked_at is not None:
            return None
        self.records[id_hash] = replace(found, revoked_at=now)
        return found

    async def revoke_subject(self, sub: str, now: datetime) -> int:
        live = [k for k, v in self.records.items() if v.sub == sub and v.revoked_at is None]
        for key in live:
            self.records[key] = replace(self.records[key], revoked_at=now)
        return len(live)

    def end_subject(self, sub: str) -> None:
        """What unlinking and the purge do to a subject's sessions, for tests."""
        self.records = {k: v for k, v in self.records.items() if v.sub != sub}


_PORT: type[UserSessionStore] = InMemoryUserSessionStore
