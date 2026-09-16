"""Discord gateway ingestion process.

Runs exactly one replica. Two containers sharing a bot token both identify to
the gateway and ingest every message twice; Discord does not error, so the
duplication is silent. See docker-compose.yml.

Nine concurrent jobs make up the process, each a loop that survives its own
failures because none of them may take the others down:

  live         messages the gateway hands us, persisted as they arrive
  backfill     paginated history, newest-first, resuming from stored cursors
  reconcile    edits and deletions that happened while we were not running
  windows      retrieval units rebuilt from newly captured messages
  embeddings   the backlog of windows without a current vector
  extraction   asks read out of newly captured conversation
  ask backlog  asks read out of everything backfill imported
  ask state    open/answered/stale, applied from observed events only
  memory       remembered conversation past its retention window, deleted

Only the live loop is real-time. The rest are catch-up work whose whole point
is that an outage costs time rather than fidelity.

Extraction runs as two jobs for one reason: the live one can only see what
the gateway hands it, and backfill imports most of a channel's history without
ever touching it. The backlog job reads those messages back out of the corpus,
so "what did people ask me to do?" answers from the whole archive rather than
from whatever arrived since the last deploy.

Extraction is here rather than on the query path on purpose: extracting when
somebody asks repeats identical work on every question, costs a full model run
each time, and can only see whatever retrieval happened to surface. It is also
the job whose failure is quietest -- a dead extractor and a quiet server look
identical from outside, because "nothing outstanding" is a plausible answer --
so it reports its own progress on the health endpoint.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
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
from chatmemory.app.asks.state import AskStateService
from chatmemory.app.asks.worker import BacklogExtractionWorker, ExtractionWorker
from chatmemory.app.conversation import MemoryRetention
from chatmemory.app.ingest import EmbeddingWorker, IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.composition import build_ask_pipeline, build_memory_retention
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
# How often open asks are re-examined against what has since been observed.
# Minutes rather than seconds: every transition comes from a reply or a
# reaction that is already stored, so a pass is a few statements over rows
# that changed, and nothing is lost by running it on a cadence.
ASK_STATE_INTERVAL_SECONDS = 300.0
# How often the backlog pass reads another slice of unextracted history.
# Together with the slice size this is the whole rate bound: extraction is a
# model call per candidate message, so history drains at a bounded cost per
# minute rather than as fast as the database can serve it. A channel that
# stopped changing months ago pays one empty index scan a minute for it.
BACKLOG_EXTRACTION_INTERVAL_SECONDS = 60.0
# How often expired conversation memory is deleted. Hourly: a window measured
# in days is honoured to within an hour, and a pass is two indexed deletes.
MEMORY_RETENTION_INTERVAL_SECONDS = 3600.0


def indexed_channels(settings: Settings) -> list[ChannelRef]:
    return [ChannelRef(PLATFORM, cid) for cid in sorted(settings.indexed_channel_ids)]


# --- the jobs -----------------------------------------------------------


async def live_loop(
    source: DiscordChatSource,
    service: IngestService,
    state: HealthState,
    extraction: ExtractionWorker | None = None,
) -> None:
    async for message in source.stream():
        try:
            if await service.capture(message):
                state.last_message_ingested_at = time.time()
                if extraction is not None:
                    # After the capture and only on success: an ask row points
                    # at the message row, so extracting from a message the
                    # store has not written has nowhere to point. `submit`
                    # neither blocks nor raises -- a backlogged extractor must
                    # cost asks, never ingestion.
                    extraction.submit(message)
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
    ready: asyncio.Event | None = None,
) -> None:
    # Reads history through the same channel cache backfill does, so it waits
    # for the same signal; starting first only produces unavailability.
    if ready is not None:
        await ready.wait()
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


async def extraction_loop(
    worker: ExtractionWorker,
    state: HealthState,
    idle: float = IDLE_SECONDS,
) -> None:
    """Extract asks from captured conversation, for as long as the process runs.

    Shaped like the embedding worker, and for the same reason: a pass that
    dies on one bad batch stops extracting entirely, and the only symptom is
    that obligations quietly stop appearing -- which reads as "nobody asked me
    anything" rather than as a fault. The worker absorbs a failing batch; this
    absorbs everything else, including a failure to reach the store at all.

    Progress is published on every iteration rather than only when something
    was extracted, so a stalled extractor is visible as a queue that is not
    draining instead of as an absence of output.
    """
    while True:
        try:
            done = await worker.run_once()
        except Exception:
            log.exception("asks.extraction_pass_failed")
            done = 0
        state.details["asks_extraction"] = worker.progress.as_dict()
        if done == 0:
            await asyncio.sleep(idle)


async def backlog_extraction_loop(
    worker: BacklogExtractionWorker,
    state: HealthState,
    interval: float = BACKLOG_EXTRACTION_INTERVAL_SECONDS,
) -> None:
    """Extract asks from history the gateway never streamed to us.

    Sleeps after every pass, busy or idle, because the interval is half of the
    rate bound: the other half is how much one pass may read. A pass that is
    allowed to run flat out drains a year of archive in an afternoon and bills
    for every message of it.

    Absorbs everything, for the reason the live pass does. A worker that dies
    on one bad batch stops extracting entirely, and the only symptom is that
    obligations quietly stop appearing -- which reads as "nobody asked me
    anything" rather than as a fault. Progress, including how much is left, is
    published every iteration so that a backlog which has stopped draining is
    visible as a number rather than as an absence of output.
    """
    while True:
        try:
            await worker.run_once()
        except Exception:
            # Including a failure to reach the store at all: the marks are in
            # the corpus, so nothing this pass did is lost by trying again.
            log.exception("asks.backlog_pass_failed")
        state.details["asks_backlog"] = worker.progress.as_dict()
        await asyncio.sleep(interval)


async def ask_state_loop(
    asks: AskStateService,
    state: HealthState,
    interval: float = ASK_STATE_INTERVAL_SECONDS,
) -> None:
    """Age and close asks from events that were observed, never from judgement.

    Without this pass an ask stays open forever: the replies and reactions
    that answer it are already in the corpus, but nothing looks at them, so
    "what do I need to do" keeps reporting work that was finished weeks ago --
    which is the fastest way to make somebody stop reading the list.
    """
    while True:
        try:
            refreshed = await asks.refresh(datetime.now(UTC))
            state.details["asks_state"] = {
                "answered_by_reply": refreshed.answered_by_reply,
                "answered_by_reaction": refreshed.answered_by_reaction,
                "marked_stale": refreshed.marked_stale,
                "last_run_at": time.time(),
            }
        except Exception:
            # State is derived, so a failed pass costs freshness rather than
            # data: the next one re-derives everything from the same rows.
            log.exception("asks.state_refresh_failed")
        await asyncio.sleep(interval)


async def memory_retention_loop(
    retention: MemoryRetention,
    state: HealthState,
    interval: float = MEMORY_RETENTION_INTERVAL_SECONDS,
) -> None:
    """Delete what people asked the assistant once it is past retention.

    Here rather than in the bot because this is the process that runs sweeps,
    and one sweeper is enough: the bot has replicas' worth of reasons to be
    restarted, and a sweep that ran only between restarts would not run.
    Runs before its first sleep, so a deploy that shortened the window takes
    effect immediately.
    """
    while True:
        try:
            purged = await retention.sweep(datetime.now(UTC))
            state.details["memory_retention"] = {
                "turns": purged.turns,
                "summaries": purged.summaries,
                "last_run_at": time.time(),
            }
        except Exception:
            # Expired rows stay until the next pass; nothing else depends on it.
            log.exception("memory.retention_failed")
        await asyncio.sleep(interval)


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

    # Built before the TaskGroup so the live loop can be handed the worker it
    # feeds. None only when an operator has switched extraction off, which is
    # said out loud below: "obligations are empty" and "extraction is off" are
    # indistinguishable from the answer side, and one of them is a decision
    # somebody made.
    asks = build_ask_pipeline(settings, engine) if settings.ask_extraction_enabled else None
    if asks is None:
        log.warning(
            "ingest.ask_extraction_disabled",
            hint="ASK_EXTRACTION_ENABLED=false; nothing will answer obligation questions",
        )
    else:
        # Where the live pass records what it has extracted, set before any
        # loop is started rather than beside the task that needs it: without
        # it the live pass leaves every captured message looking unread, and
        # the backlog pass below pays for all of it a second time.
        asks.worker.records_through(store)
        # The other half of `ask_state_loop`. That pass closes asks from
        # reactions that were recorded; this is the only thing in either
        # process that records one. Without this line the gateway receives
        # every tick the addressee gives and drops it, the pass finds nothing
        # to close, and an ask can be extracted and can never leave the list.
        handler.acknowledge_asks_with(asks.state)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(client.start(settings.discord_token.get_secret_value()))
        tasks.create_task(
            live_loop(source, service, state, asks.worker if asks else None)
        )
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
        tasks.create_task(reconcile_loop(reconciler, channels, ready=gateway_ready))

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

        # Unconditional. Retention is part of what makes remembering a
        # person's questions acceptable at all, so it is not something an
        # operator can leave switched off by omission.
        tasks.create_task(
            memory_retention_loop(build_memory_retention(settings, engine), state)
        )

        if asks is not None:
            # The point of the whole ask pipeline: without these tasks the
            # package is code nothing runs, no `ask` row is ever written, and
            # "what did people ask me today" is answered by similarity search
            # over windows -- which is the failure this feature exists to fix.
            tasks.create_task(extraction_loop(asks.worker, state))
            # And the other half: everything backfill imported, which is most
            # of a channel and none of which was ever submitted to the worker
            # above. Without this task the ask tables hold only what arrived
            # while some process happened to be running.
            tasks.create_task(
                backlog_extraction_loop(
                    BacklogExtractionWorker(
                        asks.worker,
                        store,
                        # The operator's configured scope, so a channel taken
                        # out of it stops costing model calls immediately.
                        channels=channels,
                        # The same batch size the live pass buffers to, because
                        # it means the same thing on both: how much
                        # conversation the model is shown at once.
                        batch_messages=settings.ask_extraction_window_messages,
                    ),
                    state,
                )
            )
            tasks.create_task(ask_state_loop(asks.state, state))


if __name__ == "__main__":
    asyncio.run(main())
