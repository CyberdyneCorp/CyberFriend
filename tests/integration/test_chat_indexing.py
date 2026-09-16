"""`/index` and `/unindex` against a real database.

The unit tests pin the rules over fakes. This pins what they cannot see: the
scope `IndexingService` writes is the scope a separately built `LiveScope` --
standing in for ingest -- reads back and captures under; the purge removes the
rows retrieval would have returned; and every attempt, allowed or refused,
lands in the same append-only `config_audit` table the admin console reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.acl import DiscordAclResolver
from chatmemory.adapters.discord.bot import DiscordChannelAccess
from chatmemory.adapters.store.admin_postgres import PostgresChangeRecord
from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.adapters.store.postgres import HybridSearch, PostgresStore
from chatmemory.admin.audit import ChangeKind
from chatmemory.app.indexing import IndexAction, IndexingService, IndexOutcome, IndexRequest
from chatmemory.app.ingest import IngestService
from chatmemory.app.scope import LiveScope
from chatmemory.app.windowing import WindowBuilder
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message
from chatmemory.domain.search import SearchQuery
from chatmemory.entrypoints.bot import build_indexing_stores
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember
from tests.unit.test_chat_indexing import Notifier, Perms, guild

pytestmark = pytest.mark.asyncio

ENV_CHANNEL, DESIGN = 100, 200
ADMIN, MEMBER = 1, 2
DIMS = 1536
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

BASE = {
    "discord_token": "zzz-token-zzz",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
    "indexed_channel_ids": str(ENV_CHANNEL),
}
ENVIRON = {"INDEXED_CHANNEL_IDS": str(ENV_CHANNEL)}


def ch(channel_id: int) -> ChannelRef:
    return ChannelRef("discord", channel_id)


class NoSource:
    async def backfill(self, channel: object, before: object, limit: int) -> list[Message]:
        return []


class ZeroEmbeddings:
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * DIMS for _ in texts]


def live_scope(engine: AsyncEngine) -> LiveScope:
    settings = Settings(**BASE)  # type: ignore[arg-type]
    return LiveScope.from_settings(PostgresConfigurationStore(engine), settings, ENVIRON)


def service_over(engine: AsyncEngine, scope: LiveScope) -> IndexingService:
    """The service exactly as `entrypoints/bot.py` builds its stores."""
    stores = build_indexing_stores(engine, scope)
    g = guild()
    return IndexingService(
        scope=stores.scope,
        editor=stores.editor,
        access=DiscordChannelAccess(lambda: g),
        record=stores.record,
        purges=stores.purges,
        notifier=Notifier(),
    )


async def retrievable(engine: AsyncEngine, scope: LiveScope, term: str) -> set[ChannelRef]:
    reader = PersonRef("discord", ADMIN)
    fake = FakeGuild(
        members=[FakeMember(ADMIN)],
        text_channels=[FakeChannel(ENV_CHANNEL, public=True), FakeChannel(DESIGN, public=True)],
    )
    viewer = await DiscordAclResolver(fake, scope).resolve_viewer(reader)  # type: ignore[arg-type]
    hits = await HybridSearch(engine, ZeroEmbeddings()).search(  # type: ignore[arg-type]
        viewer, SearchQuery(text=term)
    )
    return {hit.channel for hit in hits}


async def stored_rows(engine: AsyncEngine, channel_id: int) -> int:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT count(*) FROM message WHERE channel_id = :c"), {"c": channel_id}
        )
        return int(row.scalar_one())


async def test_index_then_unindex_captures_retrieves_and_withdraws(clean: AsyncEngine) -> None:
    bot_scope = live_scope(clean)
    await bot_scope.refresh()
    ingest_scope = live_scope(clean)
    await ingest_scope.refresh()
    ingest = IngestService(
        NoSource(),  # type: ignore[arg-type]
        PostgresStore(clean),
        WindowBuilder(),
        indexed_channels=ingest_scope,
    )
    service = service_over(clean, bot_scope)
    post = Message(
        platform_message_id=1,
        channel=ch(DESIGN),
        author=PersonRef("discord", MEMBER),
        content="narwhalbeam launch checklist",
        created_at=T0,
    )

    indexed = await service.handle(
        IndexRequest(PersonRef("discord", ADMIN), DESIGN, IndexAction.INDEX)
    )
    assert indexed.outcome is IndexOutcome.INDEXED

    # Ingest applies it on its next refresh, with nothing rebuilt.
    await ingest_scope.refresh()
    assert await ingest.capture(post) is True
    await ingest.rewindow(ch(DESIGN), T0 - timedelta(hours=1))
    assert await retrievable(clean, bot_scope, "narwhalbeam") == {ch(DESIGN)}

    removed = await service.handle(
        IndexRequest(PersonRef("discord", ADMIN), DESIGN, IndexAction.UNINDEX)
    )

    assert removed.outcome is IndexOutcome.UNINDEXED
    assert removed.purged >= 1
    assert await stored_rows(clean, DESIGN) == 0
    assert await retrievable(clean, bot_scope, "narwhalbeam") == set()
    await ingest_scope.refresh()
    assert await ingest.capture(post) is False


async def test_a_refused_request_is_recorded_and_stores_nothing(clean: AsyncEngine) -> None:
    scope = live_scope(clean)
    await scope.refresh()
    service = service_over(clean, scope)

    result = await service.handle(
        IndexRequest(PersonRef("discord", MEMBER), DESIGN, IndexAction.INDEX)
    )

    assert result.outcome is IndexOutcome.REFUSED
    assert await PostgresConfigurationStore(clean).load() == []
    (entry,) = await PostgresChangeRecord(clean).recent()
    assert entry.kind is ChangeKind.REFUSED
    assert entry.operator == f"discord:{MEMBER}"
    assert entry.setting == f"discord_indexing:{DESIGN}"
    assert entry.reason is not None and "Manage Channels" in entry.reason


async def test_an_allowed_change_is_recorded_beside_the_settings_own_audit_row(
    clean: AsyncEngine,
) -> None:
    scope = live_scope(clean)
    await scope.refresh()
    service = service_over(clean, scope)

    await service.handle(IndexRequest(PersonRef("discord", ADMIN), DESIGN, IndexAction.INDEX))

    entries = await PostgresChangeRecord(clean).recent()
    by_setting = {e.setting: e for e in entries}
    assert set(by_setting) == {"indexed_channel_ids", f"discord_indexing:{DESIGN}"}
    edit = by_setting["indexed_channel_ids"]
    assert edit.operator == f"discord:{ADMIN}"
    assert edit.after == f"{ENV_CHANNEL} {DESIGN}"
    assert by_setting[f"discord_indexing:{DESIGN}"].kind is ChangeKind.APPLIED


async def test_the_bot_missing_history_is_refused_against_the_real_record(
    clean: AsyncEngine,
) -> None:
    scope = live_scope(clean)
    await scope.refresh()
    stores = build_indexing_stores(clean, scope)
    g = guild(bot=Perms(read_message_history=False))
    service = IndexingService(
        scope=stores.scope,
        editor=stores.editor,
        access=DiscordChannelAccess(lambda: g),
        record=stores.record,
        purges=stores.purges,
        notifier=Notifier(),
    )

    result = await service.handle(
        IndexRequest(PersonRef("discord", ADMIN), DESIGN, IndexAction.INDEX)
    )

    assert result.missing_permission == "Read Message History"
    assert await PostgresConfigurationStore(clean).load() == []
    (entry,) = await PostgresChangeRecord(clean).recent()
    assert entry.kind is ChangeKind.REFUSED


class History:
    """A channel's live history that honours `before`, as Discord's does."""

    def __init__(self, messages: Sequence[Message]) -> None:
        self._messages = sorted(messages, key=lambda m: m.platform_message_id, reverse=True)

    async def backfill(
        self, channel: ChannelRef, before: int | None, limit: int
    ) -> list[Message]:
        older = [
            m
            for m in self._messages
            if m.channel == channel and (before is None or m.platform_message_id < before)
        ]
        return older[:limit]


async def test_reindexing_a_removed_channel_backfills_its_history_again(
    clean: AsyncEngine,
) -> None:
    # Regression: the purge left `ingest_cursor` behind, and the cursor only
    # ever moves older. A re-index then resumed from the oldest message id,
    # the source returned nothing older, and backfill reported the channel
    # complete -- its history gone for good while the bot said "backfilled
    # shortly".
    bot_scope = live_scope(clean)
    await bot_scope.refresh()
    ingest_scope = live_scope(clean)
    await ingest_scope.refresh()
    history = [
        Message(
            platform_message_id=i,
            channel=ch(DESIGN),
            author=PersonRef("discord", MEMBER),
            content=f"narwhalbeam note {i}",
            created_at=T0 + timedelta(minutes=i),
        )
        for i in range(1, 6)
    ]
    store = PostgresStore(clean)
    ingest = IngestService(
        History(history),  # type: ignore[arg-type]
        store,
        WindowBuilder(),
        indexed_channels=ingest_scope,
        page_size=2,
    )
    service = service_over(clean, bot_scope)
    admin = PersonRef("discord", ADMIN)

    await service.handle(IndexRequest(admin, DESIGN, IndexAction.INDEX))
    await ingest_scope.refresh()
    assert await ingest.backfill_channel(ch(DESIGN)) == len(history)

    await service.handle(IndexRequest(admin, DESIGN, IndexAction.UNINDEX))
    assert await stored_rows(clean, DESIGN) == 0
    assert await store.get_cursor(ch(DESIGN)) is None

    again = await service.handle(IndexRequest(admin, DESIGN, IndexAction.INDEX))
    assert again.outcome is IndexOutcome.INDEXED
    await ingest_scope.refresh()
    assert await ingest.backfill_channel(ch(DESIGN)) == len(history)
    assert await stored_rows(clean, DESIGN) == len(history)
