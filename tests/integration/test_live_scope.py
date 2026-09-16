"""Live scope against a real database: an operator's edit reaches retrieval.

The unit tests pin the rules over fakes. This pins the join they cannot see:
the value the admin console writes through `ConfigurationEditor` is the value a
separately built `LiveScope` -- standing in for the ingest and bot processes,
which share nothing with the console but the database -- reads back, and it
changes what is captured and what a viewer can retrieve, with no object rebuilt
in between.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chatmemory.adapters.discord.acl import DiscordAclResolver
from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.adapters.store.postgres import HybridSearch, PostgresStore
from chatmemory.app.configuration import ConfigurationEditor
from chatmemory.app.ingest import IngestService
from chatmemory.app.scope import LiveScope
from chatmemory.app.windowing import WindowBuilder
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message
from chatmemory.domain.search import SearchQuery
from chatmemory.ports.configuration import StoredSetting
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio

ENV_CHANNEL, ADDED = 7100, 7300
DIMS = 1536
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
ALICE = PersonRef("discord", 1)

BASE = {
    "discord_token": "zzz-token-zzz",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
    "indexed_channel_ids": str(ENV_CHANNEL),
}
ENVIRON = {"INDEXED_CHANNEL_IDS": str(ENV_CHANNEL)}


class NoSource:
    async def backfill(self, channel: object, before: object, limit: int) -> list[Message]:
        return []


class ZeroEmbeddings:
    """Windows here are never embedded, so only the lexical leg can match."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * DIMS for _ in texts]


def ch(channel_id: int) -> ChannelRef:
    return ChannelRef("discord", channel_id)


def message(mid: int, channel_id: int, content: str) -> Message:
    return Message(
        platform_message_id=mid,
        channel=ch(channel_id),
        author=ALICE,
        content=content,
        created_at=T0 + timedelta(minutes=mid),
    )


def scope_over(engine: AsyncEngine) -> LiveScope:
    """What each process builds: its own configuration, over the shared table."""
    settings = Settings(**BASE)  # type: ignore[arg-type]
    return LiveScope.from_settings(PostgresConfigurationStore(engine), settings, ENVIRON)


async def retrievable(engine: AsyncEngine, scope: LiveScope, term: str) -> set[ChannelRef]:
    guild = FakeGuild(
        members=[FakeMember(ALICE.platform_user_id)],
        text_channels=[FakeChannel(ENV_CHANNEL, public=True), FakeChannel(ADDED, public=True)],
    )
    viewer = await DiscordAclResolver(guild, scope).resolve_viewer(ALICE)
    hits = await HybridSearch(engine, ZeroEmbeddings()).search(  # type: ignore[arg-type]
        viewer, SearchQuery(text=term)
    )
    return {hit.channel for hit in hits}


def ingest(engine: AsyncEngine, scope: LiveScope) -> IngestService:
    return IngestService(
        NoSource(),  # type: ignore[arg-type]
        PostgresStore(engine),
        WindowBuilder(),
        indexed_channels=scope,
    )


async def test_a_channel_added_in_the_console_is_captured_and_retrieved_without_a_restart(
    clean: AsyncEngine,
) -> None:
    console = ConfigurationEditor(PostgresConfigurationStore(clean))
    scope = scope_over(clean)
    await scope.refresh()
    service = ingest(clean, scope)

    assert await service.capture(message(1, ADDED, "zebracorn launch plan")) is False

    await console.set("indexed_channel_ids", f"{ENV_CHANNEL} {ADDED}", "ana")
    await scope.refresh()

    assert await service.capture(message(2, ADDED, "zebracorn launch plan")) is True
    await service.rewindow(ch(ADDED), T0 - timedelta(hours=1))
    assert await retrievable(clean, scope, "zebracorn") == {ch(ADDED)}


async def test_a_channel_removed_in_the_console_leaves_retrieval_and_capture(
    clean: AsyncEngine,
) -> None:
    console = ConfigurationEditor(PostgresConfigurationStore(clean))
    scope = scope_over(clean)
    await console.set("indexed_channel_ids", f"{ENV_CHANNEL} {ADDED}", "ana")
    await scope.refresh()
    service = ingest(clean, scope)
    assert await service.capture(message(1, ADDED, "quokkafish roadmap")) is True
    await service.rewindow(ch(ADDED), T0 - timedelta(hours=1))
    assert await retrievable(clean, scope, "quokkafish") == {ch(ADDED)}

    await console.set("indexed_channel_ids", str(ENV_CHANNEL), "ana")
    await scope.refresh()

    assert await service.capture(message(2, ADDED, "quokkafish again")) is False
    assert await retrievable(clean, scope, "quokkafish") == set()


class Failover:
    """A real store whose engine can be swapped for one that cannot connect.

    What a database outage looks like to a running process: the same
    configuration object, and a driver that suddenly raises.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.inner = PostgresConfigurationStore(engine)

    async def load(self) -> Sequence[StoredSetting]:
        return await self.inner.load()

    async def put(self, key: str, raw: str, operator: str) -> None:
        await self.inner.put(key, raw, operator)

    async def clear(self, key: str, operator: str) -> None:
        await self.inner.clear(key, operator)

    async def record_refusal(self, key: str, operator: str, reason: str) -> None:
        await self.inner.record_refusal(key, operator, reason)


async def test_an_unreachable_database_keeps_the_stored_scope(clean: AsyncEngine) -> None:
    """Task 1.6 against a real driver failure, not a raised fake."""
    await ConfigurationEditor(PostgresConfigurationStore(clean)).set(
        "indexed_channel_ids", str(ADDED), "ana"
    )
    dead = create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    try:
        store = Failover(clean)
        settings = Settings(**BASE)  # type: ignore[arg-type]
        live = LiveScope.from_settings(store, settings, ENVIRON)
        await live.refresh()
        assert live.current() == frozenset({ADDED})

        store.inner = PostgresConfigurationStore(dead)
        report = await live.refresh()

        assert report.applied is False
        assert live.current() == frozenset({ADDED}), "an outage re-added a removed channel"
    finally:
        await dead.dispose()
