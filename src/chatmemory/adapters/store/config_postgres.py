"""Postgres implementation of the configuration store.

Mirrors the other store adapters: the statements live next door in
`config_sql.py`, and this binds them. It deliberately does almost nothing --
no parsing, no precedence, no validation. Those are decisions, they live in
`chatmemory.app.configuration`, and an adapter that quietly dropped a row it
could not parse would make the reporting rule ("skip it and say so")
impossible to honour, because nobody would be told.

One thing it must never do is turn a failed read into an empty result. "No
rows" means the environment is in charge of every setting; a swallowed error
reported that way would silently revert every override an operator has made,
including the narrowing ones. So `load` lets the failure out, and the caller's
refresh keeps what it already had.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import structlog
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import config_sql
from chatmemory.ports.configuration import StoredSetting

log = structlog.get_logger()


def _setting_from_row(row: RowMapping) -> StoredSetting:
    return StoredSetting(
        key=str(row["key"]),
        raw=str(row["value"]),
        updated_by=str(row["updated_by"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


class PostgresConfigurationStore:
    """Implements `ConfigurationStore`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def load(self) -> Sequence[StoredSetting]:
        async with self._engine.connect() as conn:
            result = await conn.execute(config_sql.LOAD_SETTINGS)
            return [_setting_from_row(row) for row in result.mappings()]

    async def put(self, key: str, raw: str, operator: str) -> None:
        # `begin()` rather than `connect()`: the setting and its audit row are
        # one statement, and this is what commits it.
        async with self._engine.begin() as conn:
            await conn.execute(
                config_sql.UPSERT_SETTING,
                {"key": key, "value": raw, "operator": operator, "at": datetime.now(UTC)},
            )

    async def clear(self, key: str, operator: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                config_sql.CLEAR_SETTING,
                {
                    "key": key,
                    "operator": operator,
                    # Said in the record rather than left to be inferred from
                    # an empty "after": a cleared setting and a setting set to
                    # nothing read identically a year later.
                    "reason": "cleared; the setting falls back to the environment",
                    "at": datetime.now(UTC),
                },
            )

    async def record_refusal(self, key: str, operator: str, reason: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                config_sql.RECORD_REFUSAL,
                {"key": key, "operator": operator, "reason": reason, "at": datetime.now(UTC)},
            )
