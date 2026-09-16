"""Postgres implementations of the console's credential store and change record.

Mirrors `asks_postgres.py`: the statements live next door in `admin_sql.py`,
and this binds them.

The grant and withdrawal helpers at the bottom exist because a credential and
the record of who granted it are only worth having together. Issued in one
transaction and recorded in another, a crash between the two leaves a working
console credential that nothing accounts for -- and an unaccounted credential
on this surface is precisely the thing the change record was built to make
impossible.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

import structlog
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chatmemory.adapters.store import admin_sql
from chatmemory.admin.audit import (
    ChangeKind,
    ChangeRecord,
    ChangeRecordStore,
    ConfigurationChange,
    by_shell,
)
from chatmemory.admin.auth import (
    IssuedOperatorToken,
    Operator,
    OperatorTokenRecord,
    OperatorTokens,
    generate_admin_token,
)
from chatmemory.app.tokens import hash_token

log = structlog.get_logger()


def _operator(name: str) -> Operator | None:
    """A stored name, back as an `Operator`, or None if it is not one.

    Every name goes through `Operator` on the way in, so this only fires if a
    row was written around the application. Failing closed there is the right
    direction: a credential whose operator cannot be named is a credential
    whose changes cannot be attributed, which is the one thing this surface
    must not allow.
    """
    try:
        return Operator(name)
    except ValueError:
        log.error("admin.unusable_operator_name", operator=name)
        return None


class PostgresOperatorTokens:
    """The console credential -> operator mapping, stored as hashes.

    Every write is scoped to one operator, so revoking or rotating one
    person's credential never touches anyone else's.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def operator_for_token(self, token: str) -> Operator | None:
        async with self._engine.connect() as conn:
            row = await conn.execute(
                admin_sql.LOOKUP_ADMIN_TOKEN, {"token_hash": hash_token(token)}
            )
            found = row.mappings().first()
        if found is None:
            # Unknown and revoked are the same answer on purpose: the caller
            # learns that this credential does not work, and nothing else.
            return None
        return _operator(str(found["operator"]))

    async def issue(self, operator: Operator, label: str = "") -> IssuedOperatorToken:
        token = generate_admin_token()
        digest = hash_token(token)
        async with self._engine.begin() as conn:
            await _insert_token(conn, operator, digest, label)
        return IssuedOperatorToken(
            operator=operator, token=token, token_hash=digest, label=label
        )

    async def revoke(self, operator: Operator) -> int:
        async with self._engine.begin() as conn:
            return await _revoke_tokens(conn, operator)

    async def live_credentials(self, operator: Operator) -> int:
        async with self._engine.connect() as conn:
            return await _live_credentials(conn, operator)

    async def active_tokens(self) -> Sequence[OperatorTokenRecord]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(admin_sql.ACTIVE_ADMIN_TOKENS)
            found = list(rows.mappings())
        return tuple(
            OperatorTokenRecord(
                operator=operator,
                token_hash=str(row["token_hash"]),
                label=str(row["label"]),
                issued_at=cast(datetime, row["issued_at"]),
            )
            for row, operator in ((r, _operator(str(r["operator"]))) for r in found)
            if operator is not None
        )


class PostgresChangeRecord:
    """The durable change record. Append and read; there is nothing else.

    `record` is not given an update or delete counterpart anywhere in this
    file, and migration 0012 puts a trigger behind that so a hand-written
    UPDATE at a psql prompt fails too.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(
        self, change: ConfigurationChange, *, now: datetime | None = None
    ) -> ChangeRecord:
        async with self._engine.begin() as conn:
            return await append_change(conn, change, now)

    async def recent(self, limit: int = 100) -> Sequence[ChangeRecord]:
        if limit <= 0:
            return ()
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                admin_sql.RECENT_CONFIG_AUDIT, {"limit": limit}
            )
            return tuple(_entry(row) for row in rows.mappings())


def _entry(row: RowMapping) -> ChangeRecord:
    return ChangeRecord(
        sequence=int(row["id"]),
        recorded_at=cast(datetime, row["recorded_at"]),
        operator=str(row["operator"]),
        setting=str(row["setting"]),
        kind=ChangeKind(str(row["kind"])),
        before=_optional(row["before_value"]),
        after=_optional(row["after_value"]),
        reason=_optional(row["reason"]),
    )


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


async def append_change(
    conn: AsyncConnection, change: ConfigurationChange, now: datetime | None = None
) -> ChangeRecord:
    """Write one entry on a caller's connection.

    Taking the connection rather than the engine is what lets a change and
    its record share a transaction. Exported for that reason, and for no
    other: there is still no way to express anything but an append.
    """
    row = await conn.execute(
        admin_sql.APPEND_CONFIG_AUDIT,
        {
            "operator": change.operator,
            "setting": change.setting,
            "kind": str(change.kind),
            "before_value": change.before,
            "after_value": change.after,
            "reason": change.reason,
            "recorded_at": now,
        },
    )
    written = row.mappings().one()
    return ChangeRecord(
        sequence=int(written["id"]),
        recorded_at=cast(datetime, written["recorded_at"]),
        operator=change.operator,
        setting=change.setting,
        kind=change.kind,
        before=change.before,
        after=change.after,
        reason=change.reason,
    )


async def _insert_token(
    conn: AsyncConnection, operator: Operator, digest: str, label: str
) -> None:
    await conn.execute(
        admin_sql.INSERT_ADMIN_TOKEN,
        {"token_hash": digest, "operator": operator.name, "label": label},
    )


async def _revoke_tokens(conn: AsyncConnection, operator: Operator) -> int:
    result = await conn.execute(
        admin_sql.REVOKE_OPERATOR_TOKENS, {"operator": operator.name}
    )
    return result.rowcount or 0


async def _live_credentials(conn: AsyncConnection, operator: Operator) -> int:
    row = await conn.execute(
        admin_sql.COUNT_LIVE_OPERATOR_TOKENS, {"operator": operator.name}
    )
    return int(row.scalar_one())


# --- grant and withdrawal, recorded in the same transaction ------------


def _access_setting(operator: Operator) -> str:
    return f"console_access:{operator.name}"


async def grant_console_access(
    engine: AsyncEngine, operator: Operator, label: str = ""
) -> IssuedOperatorToken:
    """Issue a credential and record the grant, atomically.

    Recorded as an escalation rather than an ordinary edit: it widens who may
    change what the agent reaches, which is the same class of act as enabling
    a mutating tool and belongs in the same filter.
    """
    token = generate_admin_token()
    digest = hash_token(token)
    async with engine.begin() as conn:
        before = await _live_credentials(conn, operator)
        await _insert_token(conn, operator, digest, label)
        await append_change(
            conn,
            by_shell(
                setting=_access_setting(operator),
                kind=ChangeKind.ESCALATION,
                before=_credentials(before),
                after=_credentials(before + 1),
                reason=f"credential issued from the operator CLI (label={label!r})",
            ),
        )
    return IssuedOperatorToken(
        operator=operator, token=token, token_hash=digest, label=label
    )


async def withdraw_console_access(engine: AsyncEngine, operator: Operator) -> int:
    """Revoke one operator's credentials and record it, atomically.

    An ordinary `APPLIED` change, not an escalation: this narrows access, and
    a narrowing filed alongside widenings would dilute the one filter that
    answers "what got more permissive, and who did it".
    """
    async with engine.begin() as conn:
        before = await _live_credentials(conn, operator)
        revoked = await _revoke_tokens(conn, operator)
        await append_change(
            conn,
            by_shell(
                setting=_access_setting(operator),
                kind=ChangeKind.APPLIED,
                before=_credentials(before),
                after=_credentials(before - revoked),
                reason="credential revoked from the operator CLI",
            ),
        )
    return revoked


def _credentials(count: int) -> str:
    return f"{count} live credential(s)"


# Checked by mypy, not by a comment: an adapter that drifts from its port
# stops type-checking rather than failing at the first request that needs the
# method somebody forgot. The console's authentication is the last place a
# partially implemented store should be discovered at run time.
_TOKENS_PORT: type[OperatorTokens] = PostgresOperatorTokens
_RECORD_PORT: type[ChangeRecordStore] = PostgresChangeRecord
