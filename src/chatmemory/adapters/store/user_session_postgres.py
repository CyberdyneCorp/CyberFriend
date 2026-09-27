"""Postgres store for the web user area's sessions: `user_session` (0036).

Hashes and ciphertext only; `admin.user.service` seals the tokens before
anything reaches this adapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import admin_sql
from chatmemory.admin.user.store import (
    RefreshedUserTokens,
    UserSessionRecord,
    UserSessionStore,
)


class PostgresUserSessionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, session: UserSessionRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                admin_sql.INSERT_USER_SESSION,
                {
                    "id_hash": session.id_hash,
                    "sub": session.sub,
                    "email": session.email,
                    "access_token_enc": session.access_token_enc,
                    "refresh_token_enc": session.refresh_token_enc,
                    "id_token_enc": session.id_token_enc,
                    "access_expires_at": session.access_expires_at,
                    "created_at": session.created_at,
                    "last_seen_at": session.last_seen_at,
                    "expires_at": session.expires_at,
                    "fresh_auth_at": session.fresh_auth_at,
                },
            )

    async def live(self, id_hash: str, now: datetime, idle: timedelta) -> UserSessionRecord | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                admin_sql.LIVE_USER_SESSION,
                {"id_hash": id_hash, "now": now, "idle_since": now - idle},
            )
            row = result.mappings().first()
        return None if row is None else _session(row)

    async def refreshed(self, id_hash: str, tokens: RefreshedUserTokens, now: datetime) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                admin_sql.REFRESH_USER_SESSION,
                {
                    "id_hash": id_hash,
                    "access_token_enc": tokens.access_token_enc,
                    "refresh_token_enc": tokens.refresh_token_enc,
                    "id_token_enc": tokens.id_token_enc,
                    "access_expires_at": tokens.access_expires_at,
                    "expires_at": tokens.expires_at,
                    "now": now,
                },
            )
        return bool(result.rowcount)

    async def touch(self, id_hash: str, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(admin_sql.TOUCH_USER_SESSION, {"id_hash": id_hash, "now": now})

    async def revoke(self, id_hash: str, now: datetime) -> UserSessionRecord | None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                admin_sql.REVOKE_USER_SESSION, {"id_hash": id_hash, "now": now}
            )
            row = result.mappings().first()
        return None if row is None else _session(row)

    async def revoke_subject(self, sub: str, now: datetime) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                admin_sql.REVOKE_USER_SESSIONS_OF, {"sub": sub, "now": now}
            )
        return int(result.rowcount)


def _session(row: RowMapping) -> UserSessionRecord:
    refresh = row["refresh_token_enc"]
    return UserSessionRecord(
        id_hash=str(row["id_hash"]),
        sub=str(row["sub"]),
        email=cast("str | None", row["email"]),
        access_token_enc=bytes(row["access_token_enc"]),
        refresh_token_enc=None if refresh is None else bytes(refresh),
        id_token_enc=bytes(row["id_token_enc"]),
        access_expires_at=cast(datetime, row["access_expires_at"]),
        created_at=cast(datetime, row["created_at"]),
        last_seen_at=cast(datetime, row["last_seen_at"]),
        expires_at=cast(datetime, row["expires_at"]),
        fresh_auth_at=cast("datetime | None", row["fresh_auth_at"]),
        revoked_at=cast("datetime | None", row["revoked_at"]),
    )


_PORT: type[UserSessionStore] = PostgresUserSessionStore
