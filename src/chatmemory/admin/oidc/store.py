"""The login and session records, their ports, and in-memory stores for tests.

Both records hold hashes and ciphertext only: the session id, the state, the
nonce and the browser binding as sha256, the verifier and the tokens as
AES-GCM under the session key. Encryption is the service's job, so an
in-memory store keeps exactly what Postgres would, and a test that asserts on
a stored plaintext fails here as it would there.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

ADMIN_PURPOSE = "admin"


@dataclass(frozen=True, slots=True)
class LoginRecord:
    """One sign-in in flight. Used once, within ten minutes."""

    state_hash: str
    nonce_hash: str
    verifier_enc: bytes
    binding_hash: str
    expires_at: datetime
    purpose: str = ADMIN_PURPOSE
    link_code_hash: str | None = None
    max_age: int | None = None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """The server half of a `__Host-cf_admin` cookie."""

    id_hash: str
    sub: str
    email: str | None
    #: For display and review. Requests take roles from the verified token.
    roles: tuple[str, ...]
    access_token_enc: bytes
    refresh_token_enc: bytes | None
    id_token_enc: bytes
    access_expires_at: datetime
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RefreshedTokens:
    roles: tuple[str, ...]
    access_token_enc: bytes
    refresh_token_enc: bytes | None
    id_token_enc: bytes
    access_expires_at: datetime
    expires_at: datetime


class LoginStore(Protocol):
    async def begin(self, login: LoginRecord, now: datetime) -> None:
        """Store a new login, dropping ones that expired."""
        ...

    async def consume(self, state_hash: str, now: datetime) -> LoginRecord | None:
        """The unexpired, unused login for this state, marked used in the same step."""
        ...


class SessionStore(Protocol):
    async def create(self, session: SessionRecord) -> None: ...

    async def live(self, id_hash: str, now: datetime, idle: timedelta) -> SessionRecord | None:
        """The session, if not revoked, not expired and seen within `idle`."""
        ...

    async def refreshed(self, id_hash: str, tokens: RefreshedTokens, now: datetime) -> None:
        """Replace the tokens after a refresh, and mark the session seen."""
        ...

    async def touch(self, id_hash: str, now: datetime) -> None: ...

    async def revoke(self, id_hash: str, now: datetime) -> SessionRecord | None:
        """Revoke a live session, returning it (for sign-out), or None."""
        ...


class InMemoryLoginStore:
    def __init__(self) -> None:
        self.records: dict[str, tuple[LoginRecord, datetime | None]] = {}

    async def begin(self, login: LoginRecord, now: datetime) -> None:
        self.records = {k: v for k, v in self.records.items() if v[0].expires_at > now}
        self.records[login.state_hash] = (login, None)

    async def consume(self, state_hash: str, now: datetime) -> LoginRecord | None:
        found = self.records.get(state_hash)
        if found is None or found[1] is not None or found[0].expires_at <= now:
            return None
        self.records[state_hash] = (found[0], now)
        return found[0]


class InMemorySessionStore:
    def __init__(self) -> None:
        self.records: dict[str, SessionRecord] = {}

    async def create(self, session: SessionRecord) -> None:
        self.records[session.id_hash] = session

    async def live(self, id_hash: str, now: datetime, idle: timedelta) -> SessionRecord | None:
        found = self.records.get(id_hash)
        if found is None or found.revoked_at is not None or found.expires_at <= now:
            return None
        return found if found.last_seen_at > now - idle else None

    async def refreshed(self, id_hash: str, tokens: RefreshedTokens, now: datetime) -> None:
        found = self.records[id_hash]
        self.records[id_hash] = replace(
            found,
            roles=tokens.roles,
            access_token_enc=tokens.access_token_enc,
            refresh_token_enc=tokens.refresh_token_enc,
            id_token_enc=tokens.id_token_enc,
            access_expires_at=tokens.access_expires_at,
            expires_at=tokens.expires_at,
            last_seen_at=now,
        )

    async def touch(self, id_hash: str, now: datetime) -> None:
        found = self.records.get(id_hash)
        if found is not None:
            self.records[id_hash] = replace(found, last_seen_at=now)

    async def revoke(self, id_hash: str, now: datetime) -> SessionRecord | None:
        found = self.records.get(id_hash)
        if found is None or found.revoked_at is not None:
            return None
        self.records[id_hash] = replace(found, revoked_at=now)
        return found


_LOGIN_PORT: type[LoginStore] = InMemoryLoginStore
_SESSION_PORT: type[SessionStore] = InMemorySessionStore
