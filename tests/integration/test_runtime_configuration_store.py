"""Stored configuration against a real database.

The unit tests pin the rules; these pin the statements, which is where the
rules are actually enforced: the audit row is written by the same statement as
the setting, the "before" it records is the value that was really displaced,
and a database that cannot be reached raises instead of reporting an empty
configuration -- the difference between "nothing is stored" and "we could not
look" being the whole failure rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.app.configuration import (
    INDEXED_CHANNEL_IDS,
    ConfigurationEditor,
    RuntimeConfiguration,
    SecretSetting,
)
from chatmemory.config import Settings
from chatmemory.ports.configuration import SettingSource, StoredSetting

pytestmark = pytest.mark.asyncio

BASE = {
    "discord_token": "zzz-token-zzz",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
}
LIVE_ENVIRON = {
    "DISCORD_TOKEN": "zzz-token-zzz",
    "DISCORD_GUILD_ID": "1",
    "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
    "LLM_API_KEY": "k",
    "INDEXED_CHANNEL_IDS": "100 200",
}


def settings(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


async def audit_rows(engine: AsyncEngine) -> Sequence[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT operator, setting, kind, before_value, after_value, reason "
                "FROM config_audit ORDER BY id"
            )
        )
        return [dict(row) for row in result.mappings()]


async def test_a_stored_setting_round_trips_with_its_attribution(clean: AsyncEngine) -> None:
    store = PostgresConfigurationStore(clean)

    await store.put("indexed_channel_ids", "100 200", "ana")
    loaded = await store.load()

    assert [(s.key, s.raw, s.updated_by) for s in loaded] == [
        ("indexed_channel_ids", "100 200", "ana")
    ]
    assert loaded[0].updated_at is not None


async def test_the_change_and_its_record_are_written_together(clean: AsyncEngine) -> None:
    store = PostgresConfigurationStore(clean)

    await store.put("indexed_channel_ids", "100 200", "ana")
    await store.put("indexed_channel_ids", "100", "bruno")

    assert await audit_rows(clean) == [
        {
            "operator": "ana",
            "setting": "indexed_channel_ids",
            "kind": "applied",
            "before_value": None,
            "after_value": "100 200",
            "reason": None,
        },
        {
            "operator": "bruno",
            "setting": "indexed_channel_ids",
            "kind": "applied",
            # The value this write actually displaced, read in the same
            # statement that displaced it.
            "before_value": "100 200",
            "after_value": "100",
            "reason": None,
        },
    ]


async def test_clearing_records_what_was_removed(clean: AsyncEngine) -> None:
    store = PostgresConfigurationStore(clean)
    await store.put("web_max_calls_per_run", "9", "ana")

    await store.clear("web_max_calls_per_run", "bruno")

    assert await store.load() == []
    cleared = (await audit_rows(clean))[-1]
    assert cleared["before_value"] == "9"
    assert cleared["after_value"] is None


async def test_clearing_a_setting_that_is_not_stored_records_nothing(
    clean: AsyncEngine,
) -> None:
    """A record of a change that did not happen is a record nobody can trust."""
    await PostgresConfigurationStore(clean).clear("web_max_calls_per_run", "ana")

    assert await audit_rows(clean) == []


async def test_a_refusal_is_recorded_without_the_value_it_refused(
    clean: AsyncEngine,
) -> None:
    store = PostgresConfigurationStore(clean)
    secret = "sk-live-do-not-store-this"

    with pytest.raises(SecretSetting):
        await ConfigurationEditor(store).set("llm_api_key", secret, "ana")

    assert await store.load() == []
    refusal = (await audit_rows(clean))[-1]
    assert refusal["kind"] == "refused"
    assert refusal["setting"] == "llm_api_key"
    assert refusal["before_value"] is None and refusal["after_value"] is None
    assert secret not in (refusal["reason"] or "")


async def test_the_record_cannot_be_rewritten(clean: AsyncEngine) -> None:
    """The record is the only review a change without a deploy ever gets."""
    await PostgresConfigurationStore(clean).put("window_max_tokens", "600", "ana")

    for statement in (
        "UPDATE config_audit SET operator = 'nobody'",
        "DELETE FROM config_audit",
    ):
        with pytest.raises(Exception):  # noqa: B017 - the driver's own error type
            async with clean.begin() as conn:
                await conn.execute(text(statement))

    assert len(await audit_rows(clean)) == 1


async def test_an_edit_reaches_a_running_configuration_on_its_next_refresh(
    clean: AsyncEngine,
) -> None:
    """Task 2.7, end to end: a narrowing applies without a restart.

    The process here is the snapshot a job consults; nothing is rebuilt, and
    no object is replaced except the snapshot itself.
    """
    store = PostgresConfigurationStore(clean)
    config = RuntimeConfiguration.from_settings(
        store, settings(indexed_channel_ids="100 200"), LIVE_ENVIRON
    )
    await config.refresh()
    assert config.current.get(INDEXED_CHANNEL_IDS) == {100, 200}
    assert config.current.source(INDEXED_CHANNEL_IDS) is SettingSource.ENVIRONMENT

    await ConfigurationEditor(store).set("indexed_channel_ids", "100", "ana")
    await config.refresh()

    assert config.current.get(INDEXED_CHANNEL_IDS) == {100}
    assert config.current.source(INDEXED_CHANNEL_IDS) is SettingSource.DATABASE
    assert config.current.values["indexed_channel_ids"].updated_by == "ana"


class LosesTheDatabase:
    """A store that works, and then stops -- as a database does mid-refresh."""

    def __init__(self, live: PostgresConfigurationStore) -> None:
        self._store = live

    def lose_it(self) -> None:
        # A real connection failure through the real driver: nothing is
        # listening on this port. A raised fake would prove only that the
        # resolver handles the exception it was handed.
        self._dead = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/none")
        self._store = PostgresConfigurationStore(self._dead)

    async def dispose(self) -> None:
        await self._dead.dispose()

    async def load(self) -> Sequence[StoredSetting]:
        return await self._store.load()

    async def put(
        self, key: str, raw: str, operator: str, *, display: str | None = None
    ) -> None:
        await self._store.put(key, raw, operator, display=display)

    async def clear(self, key: str, operator: str, *, display: str | None = None) -> None:
        await self._store.clear(key, operator, display=display)

    async def record_refusal(
        self, key: str, operator: str, reason: str, *, display: str | None = None
    ) -> None:
        await self._store.record_refusal(key, operator, reason, display=display)


async def test_an_unreachable_database_keeps_the_narrowing_in_force(
    clean: AsyncEngine,
) -> None:
    """The rule, through a real driver failure rather than a raised fake.

    A store that answered "no rows" when it could not connect would widen the
    indexing scope back to what the environment says, which is the outcome
    this whole capability is shaped to avoid.
    """
    store = LosesTheDatabase(PostgresConfigurationStore(clean))
    config = RuntimeConfiguration.from_settings(
        store, settings(indexed_channel_ids="100 200"), LIVE_ENVIRON
    )
    await ConfigurationEditor(store).set("indexed_channel_ids", "100", "ana")
    await config.refresh()
    assert config.current.get(INDEXED_CHANNEL_IDS) == {100}

    store.lose_it()
    try:
        report = await config.refresh()
    finally:
        await store.dispose()

    assert report.applied is False
    assert config.current.get(INDEXED_CHANNEL_IDS) == {100}


async def test_a_malformed_row_written_directly_is_skipped_not_obeyed(
    clean: AsyncEngine,
) -> None:
    """Nothing stops a row being written at a psql prompt during an incident."""
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO app_setting (key, value, updated_by) "
                "VALUES ('indexed_channel_ids', 'the marketing ones', 'ana')"
            )
        )
    config = RuntimeConfiguration.from_settings(
        PostgresConfigurationStore(clean), settings(indexed_channel_ids="100 200"), LIVE_ENVIRON
    )

    report = await config.refresh()

    assert config.current.get(INDEXED_CHANNEL_IDS) == {100, 200}
    assert [(p.key, p.kind) for p in report.problems] == [
        ("indexed_channel_ids", "value_rejected")
    ]


async def test_a_credential_row_written_directly_is_ignored(clean: AsyncEngine) -> None:
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO app_setting (key, value, updated_by) "
                "VALUES ('discord_token', 'stolen', 'ana')"
            )
        )
    config = RuntimeConfiguration.from_settings(
        PostgresConfigurationStore(clean), settings(), LIVE_ENVIRON
    )

    report = await config.refresh()

    assert "discord_token" not in config.current.values
    assert [(p.key, p.kind) for p in report.problems] == [("discord_token", "secret_ignored")]
