"""Indexing scope is read live: a stored change applies without a restart.

Scope used to be read once from the environment when each process started, so
the admin console's channel screen wrote a value nothing read back. These
tests drive the running pieces -- the ingest service, the backfill and
reconcile loops, the extraction ledger and the permission resolvers -- through
a `LiveScope` over a fake configuration store, change what is stored, refresh,
and check the same objects act on the new scope. Nothing is rebuilt between
the change and the assertion, because rebuilding is the restart this removes.

The last section reads the ingest entrypoint, because the failure this project
keeps having is a feature that is implemented, tested and wired to nothing.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import chatmemory
from chatmemory.adapters.discord.acl import (
    DiscordAclResolver,
    DiscordAudienceResolver,
    LiveGuild,
    PermissionCaches,
)
from chatmemory.adapters.discord.gateway import CachingAclResolver
from chatmemory.app.configuration import RuntimeConfiguration
from chatmemory.app.ingest import IngestService
from chatmemory.app.scope import (
    LiveScope,
    ScopeChange,
    ScopeProvider,
    StaticScope,
    as_scope,
)
from chatmemory.app.windowing import WindowBuilder
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message
from chatmemory.entrypoints.ingest import (
    ScopedExtractionLedger,
    backfill_loop,
    reconcile_loop,
    scope_loop,
    wake_on_widening,
)
from chatmemory.health import HealthState
from chatmemory.ports.configuration import SettingSource, StoredSetting
from chatmemory.ports.store import PendingExtraction
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

SRC = Path(chatmemory.__file__).parent

ENV_CHANNEL, ADDED, OTHER = 100, 300, 400
AT = datetime(2026, 9, 1, tzinfo=UTC)
ALICE = PersonRef("discord", 1)

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


def stored(key: str, raw: str) -> StoredSetting:
    return StoredSetting(key=key, raw=raw, updated_by="ana", updated_at=AT)


class FakeConfigurationStore:
    """Stored rows an operator edits between refreshes, or an outage."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.unreachable = False
        self.loads = 0

    def store_scope(self, *channel_ids: int) -> None:
        self.rows["indexed_channel_ids"] = " ".join(str(c) for c in channel_ids)

    async def load(self) -> Sequence[StoredSetting]:
        self.loads += 1
        if self.unreachable:
            raise ConnectionError("database went away")
        return [stored(key, raw) for key, raw in self.rows.items()]

    async def put(self, key: str, raw: str, operator: str) -> None:  # pragma: no cover
        self.rows[key] = raw

    async def clear(self, key: str, operator: str) -> None:  # pragma: no cover
        self.rows.pop(key, None)

    async def record_refusal(  # pragma: no cover
        self, key: str, operator: str, reason: str
    ) -> None:
        return None


def live_scope(
    store: FakeConfigurationStore | None = None, interval: float = 30.0
) -> tuple[LiveScope, FakeConfigurationStore]:
    store = store or FakeConfigurationStore()
    settings = Settings(**BASE)  # type: ignore[arg-type]
    return LiveScope.from_settings(store, settings, ENVIRON, interval=interval), store


def message(mid: int, channel_id: int) -> Message:
    return Message(
        platform_message_id=mid,
        channel=ch(channel_id),
        author=ALICE,
        content=f"message {mid}",
        created_at=AT + timedelta(minutes=mid),
    )


# --- the provider -------------------------------------------------------


async def test_the_environment_is_in_force_until_something_is_stored() -> None:
    scope, _ = live_scope()

    assert scope.current() == frozenset({ENV_CHANNEL})
    await scope.refresh()
    assert scope.current() == frozenset({ENV_CHANNEL})
    assert scope.status()["source"] == SettingSource.ENVIRONMENT.value


async def test_stored_scope_beats_the_environment_on_refresh() -> None:
    scope, store = live_scope()
    store.store_scope(ENV_CHANNEL, ADDED)

    await scope.refresh()

    assert scope.current() == frozenset({ENV_CHANNEL, ADDED})
    assert scope.status()["source"] == SettingSource.DATABASE.value


async def test_a_failed_refresh_keeps_the_current_scope_not_the_environment() -> None:
    """Task 1.6. The narrowing an operator made must survive a database blip."""
    scope, store = live_scope()
    store.store_scope(ADDED)  # the environment's channel removed, another added
    await scope.refresh()
    assert scope.current() == frozenset({ADDED})

    store.unreachable = True
    report = await scope.refresh()

    assert report.applied is False
    assert scope.current() == frozenset({ADDED}), "a blip re-added a removed channel"
    assert scope.status()["last_refresh_applied"] is False


async def test_a_failed_refresh_at_startup_keeps_the_environment_scope() -> None:
    """At startup the scope in force *is* the environment; failing must not empty it."""
    scope, store = live_scope()
    store.unreachable = True

    await scope.refresh()

    assert scope.current() == frozenset({ENV_CHANNEL})


async def test_a_malformed_stored_scope_keeps_the_previous_one() -> None:
    scope, store = live_scope()
    store.store_scope(ADDED)
    await scope.refresh()

    store.rows["indexed_channel_ids"] = "300 not-a-channel"
    await scope.refresh()

    assert scope.current() == frozenset({ADDED})


async def test_observers_hear_scope_moves_in_both_directions() -> None:
    scope, store = live_scope()
    heard: list[ScopeChange] = []
    scope.on_change(heard.append)

    store.store_scope(ENV_CHANNEL, ADDED)
    await scope.refresh()
    store.store_scope(ADDED)
    await scope.refresh()

    assert [(c.added, c.removed) for c in heard] == [
        (frozenset({ADDED}), frozenset()),
        (frozenset(), frozenset({ENV_CHANNEL})),
    ]


async def test_an_unrelated_setting_change_is_not_a_scope_change() -> None:
    scope, store = live_scope()
    heard: list[ScopeChange] = []
    scope.on_change(heard.append)

    store.rows["web_tools_enabled"] = "false"
    await scope.refresh()

    assert heard == []


async def test_one_failing_observer_does_not_silence_the_others() -> None:
    scope, store = live_scope()
    heard: list[ScopeChange] = []

    def broken(_change: ScopeChange) -> None:
        raise RuntimeError("cannot cope")

    scope.on_change(broken)
    scope.on_change(heard.append)
    store.store_scope(ADDED)
    await scope.refresh()

    assert [c.current for c in heard] == [frozenset({ADDED})]


async def test_the_refresh_loop_runs_on_its_period_and_survives_outages() -> None:
    scope, store = live_scope(interval=0.001)
    store.unreachable = True
    task = asyncio.create_task(scope.run_forever())
    for _ in range(200):
        await asyncio.sleep(0.001)
        if store.loads >= 3:
            break
    store.unreachable = False
    store.store_scope(ADDED)
    for _ in range(200):
        await asyncio.sleep(0.001)
        if scope.current() == frozenset({ADDED}):
            break
    task.cancel()

    assert store.loads >= 3
    assert scope.current() == frozenset({ADDED})


async def test_a_shared_configuration_serves_the_same_scope() -> None:
    """The bot builds one configuration for several settings; scope reads it."""
    store = FakeConfigurationStore()
    settings = Settings(**BASE)  # type: ignore[arg-type]
    configuration = RuntimeConfiguration.from_settings(store, settings, ENVIRON)
    scope = LiveScope(configuration)
    store.store_scope(ADDED)

    await configuration.refresh()

    assert scope.current() == frozenset({ADDED})


def test_a_literal_set_is_a_static_scope_and_a_provider_passes_through() -> None:
    static = as_scope({1, 2})
    assert isinstance(static, StaticScope)
    assert static.current() == frozenset({1, 2})

    provider = StaticScope(frozenset({3}))
    assert as_scope(provider) is provider
    assert isinstance(provider, ScopeProvider)


# --- ingest -------------------------------------------------------------


class RecordingStore:
    def __init__(self) -> None:
        self.messages: dict[int, Message] = {}
        self.dirty: dict[ChannelRef, datetime] = {}

    async def upsert_messages(self, messages: Sequence[Message]) -> int:
        for m in messages:
            self.messages[m.platform_message_id] = m
        return len(messages)

    async def mark_windows_dirty(self, channel: ChannelRef, at: datetime) -> None:
        self.dirty[channel] = at


def ingest_over(scope: ScopeProvider) -> tuple[IngestService, RecordingStore]:
    store = RecordingStore()
    service = IngestService(
        source=None,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        windows=WindowBuilder(),
        indexed_channels=scope,
    )
    return service, store


async def test_a_channel_added_to_stored_scope_is_captured_without_a_restart() -> None:
    """Task 1.5: the same service object, before and after a refresh."""
    scope, config = live_scope()
    service, store = ingest_over(scope)

    assert await service.capture(message(1, ADDED)) is False
    assert 1 not in store.messages

    config.store_scope(ENV_CHANNEL, ADDED)
    await scope.refresh()

    assert await service.capture(message(2, ADDED)) is True
    assert store.messages[2].channel == ch(ADDED)


async def test_a_channel_removed_from_stored_scope_stops_being_captured() -> None:
    scope, config = live_scope()
    service, store = ingest_over(scope)
    assert await service.capture(message(1, ENV_CHANNEL)) is True

    config.store_scope(ADDED)
    await scope.refresh()

    assert await service.capture(message(2, ENV_CHANNEL)) is False
    assert 2 not in store.messages


class SweepRecorder:
    """Stands in for the service: records which channels a sweep reached."""

    def __init__(self) -> None:
        self.swept: list[ChannelRef] = []
        self.sweeps = asyncio.Event()

    async def backfill_channel(self, channel: ChannelRef, max_pages: int = 1000) -> int:
        self.swept.append(channel)
        self.sweeps.set()
        return 0


async def wait_until(predicate, tries: int = 500) -> None:  # type: ignore[no-untyped-def]
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.001)


async def test_backfill_starts_on_a_newly_added_channel_without_waiting_a_sweep() -> None:
    """The sweep interval is an hour here; the new channel is fetched anyway."""
    scope, config = live_scope()
    service = SweepRecorder()
    wake = asyncio.Event()
    scope.on_change(wake_on_widening(wake))
    task = asyncio.create_task(
        backfill_loop(
            service,  # type: ignore[arg-type]
            scope,
            HealthState(),
            interval=3600.0,
            wake=wake,
        )
    )
    await wait_until(lambda: ch(ENV_CHANNEL) in service.swept)
    assert ch(ADDED) not in service.swept

    config.store_scope(ENV_CHANNEL, ADDED)
    await scope.refresh()
    await wait_until(lambda: ch(ADDED) in service.swept)
    task.cancel()

    assert ch(ADDED) in service.swept


async def test_a_removal_does_not_wake_backfill() -> None:
    scope, config = live_scope()
    wake = asyncio.Event()
    scope.on_change(wake_on_widening(wake))

    config.store_scope(ADDED)  # ADDED in, ENV_CHANNEL out: a widening as well
    await scope.refresh()
    assert wake.is_set()

    wake.clear()
    config.store_scope()
    await scope.refresh()
    assert not wake.is_set()


class ReconcileRecorder:
    def __init__(self) -> None:
        self.reconciled: list[ChannelRef] = []

    async def reconcile(self, channel: ChannelRef, since: datetime) -> None:
        self.reconciled.append(channel)


async def test_reconciliation_reads_scope_on_every_pass() -> None:
    scope = MutableScope({ENV_CHANNEL})
    reconciler = ReconcileRecorder()
    task = asyncio.create_task(
        reconcile_loop(reconciler, scope, interval=0.001)  # type: ignore[arg-type]
    )
    await wait_until(lambda: ch(ENV_CHANNEL) in reconciler.reconciled)
    scope.ids = frozenset({ADDED})
    reconciler.reconciled.clear()
    await wait_until(lambda: ch(ADDED) in reconciler.reconciled)
    task.cancel()

    assert ch(ADDED) in reconciler.reconciled


class MutableScope:
    def __init__(self, ids: set[int]) -> None:
        self.ids = frozenset(ids)

    def current(self) -> frozenset[int]:
        return self.ids


class LedgerRecorder:
    def __init__(self) -> None:
        self.asked: list[tuple[ChannelRef, ...]] = []

    async def messages_pending_extraction(
        self, limit: int, channels: Sequence[ChannelRef] = ()
    ) -> Sequence[PendingExtraction]:
        self.asked.append(tuple(channels))
        return []

    async def record_extraction(self, entries: Sequence[PendingExtraction]) -> int:
        return len(entries)

    async def pending_extraction_count(
        self, cap: int = 1000, channels: Sequence[ChannelRef] = ()
    ) -> int:
        self.asked.append(tuple(channels))
        return 0


async def test_backlog_extraction_reads_the_scope_in_force_not_its_startup_copy() -> None:
    scope = MutableScope({ENV_CHANNEL})
    inner = LedgerRecorder()
    ledger = ScopedExtractionLedger(inner, scope)

    await ledger.messages_pending_extraction(10, [ch(OTHER)])
    scope.ids = frozenset({ADDED})
    await ledger.pending_extraction_count(10, [ch(OTHER)])

    assert inner.asked == [(ch(ENV_CHANNEL),), (ch(ADDED),)]


async def test_the_scope_loop_publishes_its_state_for_health() -> None:
    scope, store = live_scope(interval=0.001)
    state = HealthState()
    task = asyncio.create_task(scope_loop(scope, state))
    await wait_until(lambda: "indexing_scope" in state.details)
    task.cancel()

    assert state.details["indexing_scope"]["channels"] == 1  # type: ignore[index]
    assert store.loads >= 1


# --- permission resolution ----------------------------------------------


def guild() -> FakeGuild:
    return FakeGuild(
        members=[FakeMember(ALICE.platform_user_id), FakeMember(2)],
        text_channels=[
            FakeChannel(ENV_CHANNEL, public=True),
            FakeChannel(ADDED, public=True),
        ],
    )


async def test_the_acl_resolver_uses_the_current_scope() -> None:
    """Task 1.4: retrieval is bounded by this resolver, so this is retrieval's scope."""
    scope, config = live_scope()
    resolver = DiscordAclResolver(guild(), scope)
    assert (await resolver.resolve_viewer(ALICE)).visible_channels == {ch(ENV_CHANNEL)}

    config.store_scope(ADDED)
    await scope.refresh()

    assert (await resolver.resolve_viewer(ALICE)).visible_channels == {ch(ADDED)}


async def test_a_cached_viewer_is_not_served_across_a_scope_change() -> None:
    """No gateway event announces a scope change, so the cache must notice itself."""
    scope, config = live_scope()
    caches = PermissionCaches()
    fake = guild()
    cached = CachingAclResolver(
        DiscordAclResolver(LiveGuild(lambda: fake, caches), scope),
        invalidation=caches,
        clock=lambda: 0.0,  # the TTL never expires: only scope can refresh it
    )
    caches.attach("test")
    assert (await cached.resolve_viewer(ALICE)).visible_channels == {ch(ENV_CHANNEL)}
    assert cached.size == 1

    config.store_scope(ADDED)
    await scope.refresh()

    assert (await cached.resolve_viewer(ALICE)).visible_channels == {ch(ADDED)}


async def test_a_cached_audience_is_not_served_across_a_scope_change() -> None:
    scope, config = live_scope()
    caches = PermissionCaches()
    fake = guild()
    audiences = DiscordAudienceResolver(
        LiveGuild(lambda: fake, caches), scope, clock=lambda: 0.0
    )
    caches.attach("test")
    before = await audiences.resolve_for_channel(ch(ENV_CHANNEL))
    assert before.readable_channels == {ch(ENV_CHANNEL)}
    assert audiences.size == 1

    config.store_scope(ADDED)
    await scope.refresh()

    after = await audiences.resolve_for_channel(ch(ENV_CHANNEL))
    assert after.readable_channels == {ch(ADDED)}
    private = await audiences.resolve_private(ALICE)
    assert private.readable_channels == {ch(ADDED)}


# --- the running process uses it ----------------------------------------


def _main() -> ast.AsyncFunctionDef:
    tree = ast.parse((SRC / "entrypoints" / "ingest.py").read_text())
    found = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    )
    return found


def _called(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _guarded(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(branch, ast.If | ast.Try | ast.While) and name in _called(branch)
        for branch in ast.walk(node)
    )


def test_ingest_main_builds_the_live_scope_and_runs_its_refresh_unconditionally() -> None:
    main = _main()
    for job in ("from_settings", "scope_loop", "PostgresConfigurationStore"):
        assert job in _called(main), f"ingest never calls {job}"
        assert not _guarded(main, job), f"{job} is reached only conditionally"


def test_ingest_main_hands_every_scoped_job_the_provider_not_a_copy() -> None:
    main = _main()
    source = ast.unparse(main)
    assert "indexed_channels=scope" in source
    assert "settings.indexed_channel_ids" not in source, (
        "a job still reads the environment's scope, frozen at boot"
    )
    assert "backfill_loop(service, scope" in source
    assert "wake=backfill_wake" in source
    assert "wake_on_widening(backfill_wake)" in source
    assert "reconcile_loop(reconciler, scope" in source
    assert "ScopedExtractionLedger(store, scope)" in source
