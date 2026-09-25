"""Postgres stores for console sign-in: `admin_login` and `admin_session` (0031).

The rows hold hashes and ciphertext only; encryption is done by
`admin.oidc.service` before anything reaches these adapters.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import admin_sql
from chatmemory.admin.oidc.store import (
    LoginRecord,
    LoginStore,
    RefreshedTokens,
    SessionRecord,
    SessionStore,
)


class PostgresLoginStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def begin(self, login: LoginRecord, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(admin_sql.FORGET_EXPIRED_ADMIN_LOGINS, {"now": now})
            await conn.execute(
                admin_sql.INSERT_ADMIN_LOGIN,
                {
                    "state_hash": login.state_hash,
                    "nonce_hash": login.nonce_hash,
                    "verifier_enc": login.verifier_enc,
                    "binding_hash": login.binding_hash,
                    "purpose": login.purpose,
                    "link_code_hash": login.link_code_hash,
                    "max_age": login.max_age,
                    "expires_at": login.expires_at,
                },
            )

    async def consume(self, state_hash: str, now: datetime) -> LoginRecord | None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                admin_sql.CONSUME_ADMIN_LOGIN, {"state_hash": state_hash, "now": now}
            )
            row = result.mappings().first()
        if row is None:
            return None
        return LoginRecord(
            state_hash=str(row["state_hash"]),
            nonce_hash=str(row["nonce_hash"]),
            verifier_enc=bytes(row["verifier_enc"]),
            binding_hash=str(row["binding_hash"]),
            expires_at=cast(datetime, row["expires_at"]),
            purpose=str(row["purpose"]),
            link_code_hash=cast("str | None", row["link_code_hash"]),
            max_age=cast("int | None", row["max_age"]),
        )


class PostgresSessionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, session: SessionRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                admin_sql.INSERT_ADMIN_SESSION,
                {
                    "id_hash": session.id_hash,
                    "sub": session.sub,
                    "email": session.email,
                    "roles": list(session.roles),
                    "access_token_enc": session.access_token_enc,
                    "refresh_token_enc": session.refresh_token_enc,
                    "id_token_enc": session.id_token_enc,
                    "access_expires_at": session.access_expires_at,
                    "created_at": session.created_at,
                    "last_seen_at": session.last_seen_at,
                    "expires_at": session.expires_at,
                },
            )

    async def live(self, id_hash: str, now: datetime, idle: timedelta) -> SessionRecord | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                admin_sql.LIVE_ADMIN_SESSION,
                {"id_hash": id_hash, "now": now, "idle_since": now - idle},
            )
            row = result.mappings().first()
        return None if row is None else _session(row)

    async def refreshed(self, id_hash: str, tokens: RefreshedTokens, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                admin_sql.REFRESH_ADMIN_SESSION,
                {
                    "id_hash": id_hash,
                    "roles": list(tokens.roles),
                    "access_token_enc": tokens.access_token_enc,
                    "refresh_token_enc": tokens.refresh_token_enc,
                    "id_token_enc": tokens.id_token_enc,
                    "access_expires_at": tokens.access_expires_at,
                    "expires_at": tokens.expires_at,
                    "now": now,
                },
            )

    async def touch(self, id_hash: str, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(admin_sql.TOUCH_ADMIN_SESSION, {"id_hash": id_hash, "now": now})

    async def revoke(self, id_hash: str, now: datetime) -> SessionRecord | None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                admin_sql.REVOKE_ADMIN_SESSION, {"id_hash": id_hash, "now": now}
            )
            row = result.mappings().first()
        return None if row is None else _session(row)


def _session(row: RowMapping) -> SessionRecord:
    refresh = row["refresh_token_enc"]
    return SessionRecord(
        id_hash=str(row["id_hash"]),
        sub=str(row["sub"]),
        email=cast("str | None", row["email"]),
        roles=tuple(str(r) for r in row["roles"] or ()),
        access_token_enc=bytes(row["access_token_enc"]),
        refresh_token_enc=None if refresh is None else bytes(refresh),
        id_token_enc=bytes(row["id_token_enc"]),
        access_expires_at=cast(datetime, row["access_expires_at"]),
        created_at=cast(datetime, row["created_at"]),
        last_seen_at=cast(datetime, row["last_seen_at"]),
        expires_at=cast(datetime, row["expires_at"]),
        revoked_at=cast("datetime | None", row["revoked_at"]),
    )


_LOGIN_PORT: type[LoginStore] = PostgresLoginStore
_SESSION_PORT: type[SessionStore] = PostgresSessionStore
