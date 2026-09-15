"""Discord gateway ingestion process.

Runs exactly one replica. Two containers sharing a bot token both identify to
the gateway and ingest every message twice; Discord does not error, so the
duplication is silent. See docker-compose.yml.

Five concurrent jobs make up the process, each a loop that survives its own
failures because none of them may take the others down:

  live         messages the gateway hands us, persisted as they arrive
  backfill     paginated history, newest-first, resuming from stored cursors
  reconcile    edits and deletions that happened while we were not running
  windows      retrieval units rebuilt from newly captured messages
  embeddings   the backlog of windows without a current vector

Only the live loop is real-time. The rest are catch-up work whose whole point
is that an outage costs time rather than fidelity.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import cast

import structlog
from sqlalchemy.ext.asyncio import create_async_engine

from chatmemory import logging as log_setup
from chatmemory.adapters.discord.gateway import GatewayEventHandler, IngestClient
from chatmemory.adapters.discord.source import (
    DiscordChatSource,
    DiscordHistoryReader,
    HistoryChannel,
    Reconciler,
    reconcile_window,
)
from chatmemory.adapters.llm.embeddings import OpenAICompatibleEmbeddings
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.ingest import EmbeddingWorker, IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.config import Settings, get_settings
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.messages import Message
from chatmemory.health import HealthState, spawn
from chatmemory.ports.store import Store

log = structlog.get_logger()

PLATFORM = "discord"

BACKFILL_INTERVAL_SECONDS = 900.0
RECONCILE_INTERVAL_SECONDS = 900.0
# How far back a reconciliation pass re-reads. Long enough to cover a routine
# restart or deploy, short enough that the pass stays cheap; a longer outage
# needs a wider pass run by hand.
RECONCILE_LOOKBACK = timedelta(hours=24)
WINDOW_BATCH = 500
IDLE_SECONDS = 5.0


def indexed_channels(settings: Settings) -> list[ChannelRef]:
    return [ChannelRef(PLATFORM, cid) for cid in sorted(settings.indexed_channel_ids)]


# --- the jobs -----------------------------------------------------------


async def live_loop(
    source: DiscordChatSource, service: IngestService, state: HealthState
) -> None:
    async for message in source.stream():
        try:
            if await service.capture(message):
                state.last_message_ingested_at = time.time()
        except Exception:
            # One unstorable message must not end live capture. Reconciliation
            # re-reads recent history, so the loss is repaired rather than
            # permanent.
            log.exception("ingest.capture_failed", message_id=message.platform_message_id)
        state.details["live_queue"] = source.pending


async def backfill_loop(
    service: IngestService,
    channels: Sequence[ChannelRef],
    state: HealthState,
    interval: float = BACKFILL_INTERVAL_SECONDS,
    ready: asyncio.Event | None = None,
) -> None:
    """Import history, once the gateway can answer questions about channels.

    Waiting is not an optimisation. Channel lookups read discord.py's cache,
    which an identified connection populates, so a sweep that starts first
    finds every channel missing -- and logs it as unavailable, which reads
    like a permissions failure rather than a race. Without the wait the
    corpus stays empty until the next sweep, a quarter of an hour later.
    """
    if ready is not None:
        await ready.wait()
    completed = time.time()
    while True:
        state.backfill_lag_seconds = time.time() - completed
        for channel in channels:
            try:
                imported = await service.backfill_channel(channel)
            except Exception:
                # The cursor only advances after a page is written, so the
                # next sweep resumes exactly where this one stopped.
                log.exception("backfill.failed", channel=str(channel))
                continue
            if imported:
                log.info("backfill.channel", channel=str(channel), imported=imported)
        completed = time.time()
        state.backfill_lag_seconds = 0.0
        await asyncio.sleep(interval)


async def reconcile_loop(
    reconciler: Reconciler,
    channels: Sequence[ChannelRef],
    interval: float = RECONCILE_INTERVAL_SECONDS,
    lookback: timedelta = RECONCILE_LOOKBACK,
) -> None:
    # Runs before its first sleep: the moment a process starts is exactly when
    # the events it missed are waiting to be discovered.
    while True:
        since = reconcile_window(lookback)
        for channel in channels:
            try:
                await reconciler.reconcile(channel, since)
            except Exception:
                log.exception("reconcile.failed", channel=str(channel))
        await asyncio.sleep(interval)


# Re-forming from exactly the dirty watermark would cut the window that
# straddles it. Reaching back one gap means the preceding conversation is
# re-formed with it, so a message arriving late still merges into the window
# it belongs to rather than starting a new one.
REWINDOW_LOOKBACK = timedelta(minutes=30)


async def rebuild_pending_windows(
    service: IngestService, store: Store, batch: int = WINDOW_BATCH
) -> int:
    """Re-form the windows of every channel marked dirty.

    Driven by a per-channel watermark rather than by "this message has no
    window". The latter can only fire once per message, which left edited
    messages holding their pre-edit text forever and made every message its
    own single-message window -- defeating the reason retrieval embeds
    windows at all.
    """
    dirty = await store.dirty_channels()
    rebuilt = 0
    for entry in dirty:
        if not service.is_indexed(entry.channel):
            await store.clear_windows_dirty(entry.channel, entry.generation)
            continue
        rebuilt += await service.rewindow(entry.channel, entry.since - REWINDOW_LOOKBACK)
        # Conditional on the generation read above: a change that arrived
        # while the rebuild ran leaves the mark in place for the next pass.
        await store.clear_windows_dirty(entry.channel, entry.generation)

    if rebuilt == 0:
        # Safety net for rows that predate the watermark, such as a backfill
        # that landed while an older build was running.
        pending = await store.messages_without_window(batch)
        by_channel: dict[ChannelRef, list[Message]] = {}
        for message in pending:
            by_channel.setdefault(message.channel, []).append(message)
        for channel, messages in by_channel.items():
            rebuilt += await service.rebuild_windows(channel, messages)
    return rebuilt


async def window_loop(
    service: IngestService,
    store: Store,
    batch: int = WINDOW_BATCH,
    idle: float = IDLE_SECONDS,
) -> None:
    while True:
        try:
            rebuilt = await rebuild_pending_windows(service, store, batch)
        except Exception:
            log.exception("windows.rebuild_failed")
            rebuilt = 0
        if rebuilt == 0:
            await asyncio.sleep(idle)


# --- wiring -------------------------------------------------------------


async def main() -> None:
    log_setup.configure()
    settings = get_settings()
    state = HealthState()
    spawn(state, settings.health_port)

    channels = indexed_channels(settings)
    log.info(
        "ingest.starting",
        guild_id=settings.discord_guild_id,
        indexed_channels=len(channels),
        health_port=settings.health_port,
    )
    if not channels:
        # Indexing is opt-in: an empty scope means the corpus stays empty.
        # Surfaced loudly because "the bot is running but indexes nothing"
        # otherwise looks identical to "the bot is broken".
        log.warning("ingest.no_indexed_channels", hint="set INDEXED_CHANNEL_IDS")

    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    # The adapter boundary, annotated rather than cast: the type checker is
    # what proves PostgresStore still satisfies every method the jobs below
    # call. A cast here once hid a store missing window persistence, and the
    # process started anyway -- capturing messages that nothing ever windowed,
    # embedded or retrieved, with every health check green.
    store: Store = PostgresStore(engine)

    client: IngestClient | None = None

    def channel_provider(channel_id: int) -> HistoryChannel | None:
        # Late-bound: the source is built before the gateway connects, and a
        # cold cache must read as "not reachable yet", never as "no history".
        if client is None:
            return None
        return cast("HistoryChannel | None", client.get_channel(channel_id))

    source = DiscordChatSource(DiscordHistoryReader(channel_provider))
    service = IngestService(
        source=source,
        store=store,
        windows=WindowBuilder(
            max_messages=settings.window_max_messages,
            max_tokens=settings.window_max_tokens,
            gap=timedelta(seconds=settings.window_gap_seconds),
        ),
        indexed_channels=settings.indexed_channel_ids,
    )

    gateway_ready = asyncio.Event()

    def connection_changed(connected: bool) -> None:
        state.gateway_connected = connected
        if connected:
            gateway_ready.set()

    handler = GatewayEventHandler(sink=service, feed=source)
    client = IngestClient(handler, settings.discord_guild_id, connection_changed)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(client.start(settings.discord_token.get_secret_value()))
        tasks.create_task(live_loop(source, service, state))
        tasks.create_task(backfill_loop(service, channels, state, ready=gateway_ready))

        # Unconditional, like windowing and embedding below, and for the same
        # reason. This used to start only if the store was probed and found to
        # carry `stored_revisions`; no store did, so the probe failed, one
        # line was logged at boot and the process ran for good without ever
        # reconciling. Edits and deletions missed during a deploy were never
        # repaired -- a message deleted while the process was down stayed
        # retrievable indefinitely, with every health check green. `Store`
        # now declares the method, so the type checker proves at the wiring
        # site what the probe used to discover at runtime and discard.
        reconciler = Reconciler(source=source, ledger=store, sink=service)
        tasks.create_task(reconcile_loop(reconciler, channels))

        # Retrieval exists only if these two run, so a store that cannot serve
        # them must stop the process rather than let it capture into a corpus
        # nothing can ever read back.
        tasks.create_task(window_loop(service, store))

        worker = EmbeddingWorker(
            store,
            OpenAICompatibleEmbeddings(
                api_key=settings.llm_api_key.get_secret_value(),
                base_url=settings.llm_base_url,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
            ),
        )
        tasks.create_task(worker.run_forever(IDLE_SECONDS))


if __name__ == "__main__":
    asyncio.run(main())
