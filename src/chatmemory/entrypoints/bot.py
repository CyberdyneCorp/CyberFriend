"""Discord bot process.

Runs the conversational surface. Ingestion is a separate process; this one
only answers questions.

Almost nothing is wired here: the object graph comes from `composition`, which
is also where the two checks that can refuse the deployment live. That ordering
matters -- a model that cannot honour a response schema, or an embedding
model whose vectors are the wrong width, stops this process before it
identifies to the gateway rather than after people start asking it things.

Two things are decided here rather than down there, and both are properties of
the *process* rather than of the graph.

Where permission caches get their invalidation from: the guild provider this
file hands `composition` is a `LiveGuild`, so the audience cache built down
there is registered against this client's events. Hand `composition` a bare
callable instead and nothing caches -- slower, never stale.

And where a correction is written. The ask store is built with the answer
stack and the ask service is built from guild state, so this is the only place
that holds both ends; `/resolve` is registered either way, and a process that
skips this refuses every attempt to close an ask rather than silently
pretending to have recorded one.

Conversation memory is handed down the same way, and for the same reason: it
is built over the answer stack's engine, and the ask service that recalls and
remembers is built from guild state. `assemble` -> `build_bot` ->
`build_ask_service` -> `AskService(conversations=...)` is the whole chain, and
`tests/unit/test_memory_integration.py` reads it from this file down.

Personal facts follow the same route: `assemble` -> `build_personal_facts` over
the answer stack's engine -> `build_bot(facts=...)` -> `build_ask_service` ->
`AskService(facts=...)`, and `tests/unit/test_facts_behaviour.py` reads that
chain from this file down.

Catch-up summaries arrive the same way: `assemble` -> `build_catch_up` over the
answer stack's `search` and `chat` -> `build_bot(catchup=...)` ->
`build_ask_service` -> `AskService(catchup=...)`, and
`tests/unit/test_catchup_wiring.py` reads that chain from this file down. A
process that skips it answers "what did I miss in #x" by searching the corpus
for those words -- the behaviour before the feature, never a wider one.
"What did Ana say about X" follows it: `assemble` -> `build_said_by` ->
`build_bot(said_by=...)` -> `build_ask_service` -> `AskService(said_by=...)`,
read from this file down by `tests/unit/test_said_by_wiring.py`. Both answer
before the answer chain and its tracer seam, so the stack's tracer follows them:
`assemble` -> `build_bot(tracer=stack.tracer)` -> `build_ask_service` ->
`AskService(tracer=...)`, and `tests/e2e/test_trace_features.py` proves both
are exported.

Indexing scope is live here as it is in ingest. `assemble` builds a `LiveScope`
over the answer stack's engine, refreshes it before identifying, hands it to
`build_bot` -> `build_ask_service`, where both the ACL and the audience
resolver ask it on every question, and `main` runs its refresh loop beside
the gateway. Handed the startup set instead, the bot kept answering from a channel
an operator removed and never from one they added, while ingest -- which did
read the store -- captured the new one. `test_scope_and_market_wiring` reads
that chain from this file down.

Notifications are the one thing this process does that nobody asked for, and
they are produced somewhere else. Ingest extracts obligations and writes queue
rows; this process holds the only connection a person can be messaged through,
so it drains them: `assemble` -> `build_bot(notifications=stack.engine)` ->
`build_delivery`, which attaches `/notifications` to the client and builds a
`NotificationDelivery` over a `DiscordAclResolver` on this client's live guild
state, and then `main` runs `notification_loop` beside the gateway.
`tests/unit/test_notifications_wiring.py` reads that chain from this file down.
The permission re-check is the resolver: a recipient's readable channels are
resolved at the moment of sending and bound into the query, so access revoked
between extraction and delivery removes the obligation from the message.

`/index` and `/unindex` act over that same `LiveScope`. `assemble` builds
`IndexingStores` over the answer stack's engine -- the configuration editor
the admin console writes through, the change record it reads, and the
message and document stores' existing channel purges -- and hands them to
`build_bot`, which attaches an `IndexingService` to the client with a
permission resolver and a notifier over this client's live guild cache. A
scope written there is re-read by this process at once and by ingest within
its refresh period. `test_chat_indexing_wiring` reads that chain.

Position alerts ride on the scheduled-task path without its model run:
`assemble` -> `build_bot(alert_transport=edges.http_transport)` ->
`build_alert_runner`, which reads the chain through the process's transport
and sends through the same `DiscordTaskMessenger`, and `main` runs
`alert_loop` beside the scheduled sweep on the edges' clock. Off by default:
without `ALERTS_ENABLED` (and an Infura key) `BotGraph.alerts` is None and
nothing starts.

Voice questions take the same edges: `assemble` ->
`build_voice_questions(settings, engine, edges.http_transport, edges.clock)`
-> `build_bot(voice=...)` -> `CyberFriendClient.attach_voice`. The CDN
download and the transcription both go through the process's transport. Off
by default: without `VOICE_QUESTIONS_ENABLED` nothing is built, and a voice
message in a DM is answered that voice is not enabled here.

`main` and `assemble` are split at the network. `assemble(settings, edges)`
builds the whole object graph -- every chain above -- over an `Edges` value
holding the models, the embedding endpoint and the database engine, and
starts nothing. `main` hands it `Edges.production(settings)` and then connects
the gateway and runs the loops. An end-to-end test hands it fakes instead, so
the graph it drives is the one a deployment runs rather than a copy of it.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, cast

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory import logging as log_setup
from chatmemory.adapters.discord.acl import LiveGuild, PermissionCaches, _Guild
from chatmemory.adapters.discord.bot import (
    CyberFriendClient,
    DiscordChannelAccess,
    DiscordIndexNotifier,
    DiscordNotificationSender,
    DiscordTaskMessenger,
    _IndexingGuild,
)
from chatmemory.adapters.discord.gateway import attach_permission_listeners
from chatmemory.adapters.documents.store import PostgresDocumentStore
from chatmemory.adapters.store.admin_postgres import PostgresChangeRecord
from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.admin.audit import ChangeRecordStore
from chatmemory.app.alerts import AlertRunner
from chatmemory.app.ask import AskService
from chatmemory.app.asks.corrections import CorrectionService
from chatmemory.app.asks.obligations import discord_message_url
from chatmemory.app.authorization import ConfirmationLedger
from chatmemory.app.catchup import CatchUpService
from chatmemory.app.channel_listing import ChannelListingService
from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.configuration import ConfigurationEditor
from chatmemory.app.conversation import Conversations
from chatmemory.app.facts import PersonalFactsService
from chatmemory.app.indexing import ChannelPurge, IndexingService
from chatmemory.app.notifications import NotificationDelivery
from chatmemory.app.reasoning.contract import RunTracer
from chatmemory.app.said_by import SaidByService
from chatmemory.app.schedules import ScheduledTaskRunner
from chatmemory.app.scope import LiveScope, ScopeProvider
from chatmemory.app.self_description import Capabilities
from chatmemory.app.tracing_notice import TracingNotice
from chatmemory.app.voice import VoiceQuestions
from chatmemory.composition import (
    AnswerStack,
    Edges,
    ask_policy,
    build_alert_requests,
    build_alert_runner,
    build_answer_stack,
    build_ask_service,
    build_catch_up,
    build_channel_listing,
    build_conversations,
    build_feature_requests,
    build_live_scope,
    build_notification_delivery,
    build_notification_preferences,
    build_personal_facts,
    build_privacy,
    build_said_by,
    build_schedules,
    build_task_runner,
    build_tracing_notice,
    build_voice_questions,
)
from chatmemory.config import Settings, get_settings
from chatmemory.domain.identity import ChannelRef
from chatmemory.health import HealthState, spawn
from chatmemory.ports.answers import AnswerService
from chatmemory.ports.store import SearchBackend

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class IndexingStores:
    """What `/index` needs that is not guild state.

    `scope` is the process's own `LiveScope`, not a second one, so the
    refresh an indexing change triggers is the refresh retrieval reads.
    """

    scope: LiveScope
    editor: ConfigurationEditor
    record: ChangeRecordStore
    purges: tuple[ChannelPurge, ...]


class _DocumentPurge:
    """The document store's channel purge, under the name the service calls."""

    def __init__(self, documents: PostgresDocumentStore) -> None:
        self._documents = documents

    async def purge_channel(self, channel: ChannelRef) -> int:
        return await self._documents.purge_channel_documents(channel)


def build_indexing_stores(engine: AsyncEngine, scope: LiveScope) -> IndexingStores:
    """The stores behind `/index`, over the engine the answer stack holds.

    The purges are the ones retention and opt-out already use: `PURGE_CHANNEL`
    for messages, windows and tombstones, and the documents' channel purge for
    attachments shared there.
    """
    return IndexingStores(
        scope=scope,
        editor=ConfigurationEditor(PostgresConfigurationStore(engine)),
        record=PostgresChangeRecord(engine),
        purges=(PostgresStore(engine), _DocumentPurge(PostgresDocumentStore(engine))),
    )


@dataclass(frozen=True, slots=True)
class BotGraph:
    """The client and the ask service it drives, plus their invalidation source.

    Returned as one value because they are the same arrangement seen from two
    sides: the questions the ask service answers are scoped by caches that
    only this client's events can clear. Building them apart is how the MCP
    server ended up with a cache nothing invalidated.
    """

    client: CyberFriendClient
    asks: AskService
    caches: PermissionCaches
    indexing: IndexingService | None = None
    # The sweep that runs due scheduled tasks. None when the feature is off,
    # which is also when the commands say it is unavailable.
    tasks: ScheduledTaskRunner | None = None
    #: The position-alert sweep. None unless `ALERTS_ENABLED` with an Infura
    #: key; alerts are created by asking and confirming, under the same switch.
    alerts: AlertRunner | None = None
    #: The queue drain. None when no engine was handed in, or when an
    #: operator has switched notifications off -- in which case nothing in
    #: this process sends anything, which is the safe half.
    notifications: NotificationDelivery | None = None


def build_bot(
    settings: Settings,
    answers: AnswerService,
    search: SearchBackend | None = None,
    confirmations: ConfirmationLedger | None = None,
    corrections: CorrectionService | None = None,
    conversations: Conversations | None = None,
    scope: ScopeProvider | None = None,
    indexing: IndexingStores | None = None,
    facts: PersonalFactsService | None = None,
    catchup: CatchUpService | None = None,
    said_by: SaidByService | None = None,
    tracer: RunTracer | None = None,
    tracing_notice: TracingNotice | None = None,
    notifications: AsyncEngine | None = None,
    alert_transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = utc_now,
    voice: VoiceQuestions | None = None,
    capabilities: Capabilities | None = None,
) -> BotGraph:
    """Assemble the Discord surface over an already-verified answer service."""
    # Resolvers read live guild state, which does not exist until the
    # gateway connects. They take a provider and treat a cold cache as an
    # empty guild, so permission resolution fails closed during startup.
    client: CyberFriendClient | None = None
    caches = PermissionCaches()

    def provider() -> _Guild | None:
        if client is None:
            return None
        # discord.Guild satisfies _Guild in practice, but not structurally:
        # permissions_for accepts Member | Role rather than any _Member, so
        # the protocol is contravariantly incompatible. The cast is the
        # adapter boundary -- the protocol exists so the core stays testable
        # against fakes, not to re-describe discord.py's type hierarchy.
        return cast(_Guild | None, client.get_guild(settings.discord_guild_id))

    # Alert requests: the creation read through `alert_transport`, the first
    # check timed by `clock`. None unless alerts are on with an Infura key, and
    # then a request is answered that alerts are not available here.
    alert_requests = (
        build_alert_requests(settings, notifications, alert_transport, clock)
        if notifications is not None
        else None
    )
    asks = build_ask_service(
        settings,
        LiveGuild(provider, caches),
        answers,
        search=search,
        # The federation's confirmation ledger, so the desk this ask service
        # opens writes where the invoke-time gate reads. Without it a mutating
        # tool is refused for want of a confirmation nobody can ever obtain --
        # the prompt would be built inside the run and shown to no one.
        confirmations=confirmations,
        # The person's own conversation, recalled before answering and
        # remembered after. Without it every follow-up arrives with no context.
        conversations=conversations,
        # Live indexing scope, asked on every resolution. Without it the
        # resolvers fall back to the environment's startup set.
        scope=scope,
        # The asker's own facts. Without it "call me Leo" is answered that this
        # deployment remembers nothing, and `/forget` everywhere has no facts
        # to delete.
        facts=facts,
        # Catch-up summaries. Without it "what did I miss in #x" is answered
        # by searching the corpus for those words, which is the behaviour
        # this process had before the feature existed.
        catchup=catchup,
        # "What did Ana say about X". Without it that question is answered by
        # the ordinary search, which is what this process did before.
        said_by=said_by,
        # The tracer the answer stack exports through. Catch-up and said-by
        # answer before that stack, so without it neither is ever traced.
        tracer=tracer,
        # The one-time "your questions are recorded" notice on a traced reply.
        tracing_notice=tracing_notice,
        alerts=alert_requests,
    )
    if corrections is not None:
        # `/resolve` is registered either way, so without this every attempt to
        # close an ask is refused -- which is the safe half of the feature, and
        # the half a deployment that wires nothing should get.
        asks.attach_corrections(corrections)
    client = CyberFriendClient(asks, settings.discord_guild_id)
    attach_permission_listeners(client, caches)
    if capabilities is not None:
        # What a bare mention describes: the answer stack's own value, so it
        # and "what can you do?" name the same features.
        client.attach_capabilities(capabilities)
    if voice is not None:
        # Voice messages in a DM. Without it they are answered that voice is
        # not enabled here, and nothing is downloaded.
        client.attach_voice(voice)
    if alert_requests is not None:
        # `/alert list|delete` and the Confirm button. Without it both say
        # alerts are unavailable here.
        client.attach_alerts(alert_requests)
    service = None
    if indexing is not None:
        # Without this `/index` and `/unindex` are registered and say they are
        # unavailable -- the safe half, and the half a deployment wiring
        # nothing gets.
        service = build_indexing_service(client, settings.discord_guild_id, indexing)
        client.attach_indexing(service)
    listing = None
    if scope is not None:
        # `/channels` reads the same live scope `/index` writes, through the
        # same resolver the ask path scopes retrieval with. Without it the
        # command is registered and says listing is unavailable.
        listing = build_channel_listing_service(client, settings, scope)
        client.attach_channel_listing(listing)

    # `/schedule` and the sweep that runs what it creates. Built together, so a
    # deployment cannot end up with the commands and no runner -- which would
    # create tasks nothing ever performs, silently, which is exactly what this
    # feature already looks like when it is working.
    #
    # `notifications` is the answer stack's engine; it is named for the first
    # thing that needed one.
    schedules = build_schedules(settings, notifications) if notifications else None
    runner = None
    if schedules is not None and notifications is not None:
        client.attach_schedules(schedules)
        runner = build_task_runner(
            settings, notifications, asks, DiscordTaskMessenger(client.fetch_user)
        )
    if notifications is not None:
        # `/suggest` and `/suggestions`. Without it both say suggestions cannot
        # be taken here.
        client.attach_feature_requests(build_feature_requests(notifications, clock))
        # `/privacy`. Archive coverage is the `/channels` listing, so a channel
        # the person cannot read is neither named nor counted; without a
        # listing no channel is. Without the engine the command says it cannot
        # show anything here.
        client.attach_privacy(build_privacy(settings, notifications, listing, clock))
    # Position alerts: the chain read through `alert_transport` (the process's
    # edge), the message through the scheduled-task messenger with no heading
    # of its own, since an alert's text carries one in its own language.
    alerts = (
        build_alert_runner(
            settings,
            notifications,
            DiscordTaskMessenger(client.fetch_user, prefix=""),
            transport=alert_transport,
        )
        if notifications is not None
        else None
    )
    delivery = build_delivery(settings, client, provider, caches, notifications, scope)
    return BotGraph(
        client=client,
        asks=asks,
        caches=caches,
        indexing=service,
        notifications=delivery,
        tasks=runner,
        alerts=alerts,
    )


def build_delivery(
    settings: Settings,
    client: CyberFriendClient,
    provider: Callable[[], _Guild | None],
    caches: PermissionCaches,
    engine: AsyncEngine | None,
    scope: ScopeProvider | None,
) -> NotificationDelivery | None:
    """Attach `/notifications`, and build the drain that sends them.

    Two halves with different conditions, on purpose.

    The switch is attached whenever there is an engine, even where the
    operator has turned the feature off: the one thing a person may do to this
    feature is stop it, and a command that answers "unavailable" on a
    deployment that has ever sent them anything is not a way out.

    The drain is built only when notifications are enabled, and only here.
    This process is the only one that can reach a person: the queue is filled
    in ingest, where extraction runs, and is drained here, where the gateway
    connection is. Without this the whole feature is rows accumulating in a
    table nothing reads.

    The sender is given `client.fetch_user` rather than a cached user, and the
    permission resolver is built over the same live guild state and indexing
    scope every answer is bounded by -- which is what makes the check a
    send-time check rather than a repeat of one made at extraction.
    """
    if engine is None:
        return None
    client.attach_notifications(build_notification_preferences(engine))
    if not settings.notifications_enabled:
        log.warning(
            "bot.notifications_disabled",
            hint="NOTIFICATIONS_ENABLED=false; nothing queued will be delivered",
        )
        return None
    return build_notification_delivery(
        settings,
        engine,
        LiveGuild(provider, caches),
        DiscordNotificationSender(
            client.fetch_user, discord_message_url(settings.discord_guild_id)
        ),
        scope=scope,
    )


def build_channel_listing_service(
    client: CyberFriendClient, settings: Settings, scope: ScopeProvider
) -> ChannelListingService:
    """`/channels` over this client's live guild cache.

    Late-bound like the others: the guild is read when somebody asks, so a
    listing reflects access as it is then rather than as it was at startup.
    """

    def guild() -> Any:
        return client.get_guild(settings.discord_guild_id)

    return build_channel_listing(guild, scope)


def build_indexing_service(
    client: CyberFriendClient, guild_id: int, stores: IndexingStores
) -> IndexingService:
    """The indexing use case over this client's live guild cache."""

    def guild() -> _IndexingGuild | None:
        # Same adapter-boundary cast as the ACL provider: discord.Guild is the
        # real thing, the protocol is what lets tests drive it with fakes.
        return cast(_IndexingGuild | None, client.get_guild(guild_id))

    return IndexingService(
        scope=stores.scope,
        editor=stores.editor,
        access=DiscordChannelAccess(guild),
        record=stores.record,
        purges=stores.purges,
        # Late-bound like `guild`: the channel cache is read when the notice
        # is posted, not captured when the service is built.
        notifier=DiscordIndexNotifier(lambda channel_id: client.get_channel(channel_id)),
    )


async def scope_loop(scope: LiveScope, state: HealthState) -> None:
    """Keep indexing scope current, and show on the health endpoint that it is.

    `refresh` never raises and keeps the scope in force on a failed read, so
    this loop does not end on a database blip. If it ends anyway, `main`
    gathers it with the gateway and the process exits -- a restart is better
    than answering from a scope silently frozen at the moment the loop died.
    """
    while True:
        await scope.refresh()
        state.details["indexing_scope"] = scope.status()
        await asyncio.sleep(scope.interval)


#: How often the queue is drained. Seconds, not minutes: the batching window
#: is what decides how long an obligation waits, and this only decides how
#: long after that window it takes to notice. A pass over an empty queue is
#: one indexed scan that finds nothing.
NOTIFICATION_DRAIN_INTERVAL_SECONDS = 15.0


async def scheduled_task_loop(
    runner: ScheduledTaskRunner,
    state: HealthState,
    interval: float = 300.0,
    ready: asyncio.Event | None = None,
    clock: Clock = utc_now,
) -> None:
    """Run the questions people asked to have asked on their behalf.

    Waits for the gateway for the same reason the notification drain does, and
    it matters more here: a run resolves the owner's readable channels from
    live guild state, and before the connection identifies every person
    resolves to nothing readable. A pass in that state would answer every task
    with "I found nothing" -- which this feature renders as silence, so the
    schedule would advance and nobody would ever know.

    Absorbs everything. A sweep that dies stops the feature, and the symptom is
    silence, which is also what a working sweep looks like on a quiet day.
    """
    if ready is not None:
        await ready.wait()
    while True:
        try:
            sent = await runner.run_due(clock())
            state.details["scheduled_tasks"] = {
                "delivered": sent,
                "last_run_at": time.time(),
            }
        except Exception:
            # Nothing is lost that was not already: a claimed task has had its
            # schedule advanced, so it runs again next interval.
            log.exception("schedules.sweep_failed")
        await asyncio.sleep(interval)


async def notification_loop(
    delivery: NotificationDelivery,
    state: HealthState,
    interval: float = NOTIFICATION_DRAIN_INTERVAL_SECONDS,
    ready: asyncio.Event | None = None,
    clock: Clock = utc_now,
) -> None:
    """Send what ingest queued, to the people it was addressed to.

    Waits for the gateway first, and that is not an optimisation. The
    permission re-check reads live guild state; before the connection
    identifies, every member resolves to nothing readable, and a pass in that
    state would find nothing to say to anybody and log it. Worse, it is the
    one moment where "they can read no channels" is a lie rather than a fact.

    Absorbs everything. A drain that dies stops the only feature in the
    system that speaks first, and the symptom is silence -- which is what the
    feature looks like when nobody has been asked anything. So progress is
    published on the health endpoint every iteration, including the count of
    notifications dropped because access had been revoked.
    """
    if ready is not None:
        await ready.wait()
    while True:
        try:
            report = await delivery.deliver(clock())
            state.details["notifications"] = {
                **report.as_dict(),
                "last_run_at": time.time(),
            }
        except Exception:
            # Nothing is lost: an unsent row stays pending, and the expiry
            # bound in the sweep stops that being for ever.
            log.exception("notifications.drain_failed")
        await asyncio.sleep(interval)


async def alert_loop(
    runner: AlertRunner,
    state: HealthState,
    interval: float = 300.0,
    ready: asyncio.Event | None = None,
    clock: Clock = utc_now,
) -> None:
    """Check position alerts, and message the people whose positions changed.

    Waits for the gateway, because a message is the only thing a sweep can
    produce and the connection is how it is sent. Absorbs everything, as the
    scheduled sweep does: a claimed alert has already been advanced, so it is
    simply read again next sweep.
    """
    if ready is not None:
        await ready.wait()
    while True:
        try:
            sent = await runner.run_due(clock())
            state.details["position_alerts"] = {"delivered": sent, "last_run_at": time.time()}
        except Exception:
            log.exception("alerts.sweep_failed")
        await asyncio.sleep(interval)


async def run_beside_scope(
    work: Coroutine[Any, Any, None],
    scope: LiveScope,
    state: HealthState,
    extra: Sequence[Coroutine[Any, Any, None]] = (),
) -> None:
    """Run the process's work with the scope refresh loop, ending with whichever ends.

    Not `gather`: when the gateway returns, the process must exit rather than
    sit refreshing a scope nothing uses; when the loop dies, the process must
    exit rather than serve a frozen scope. Either way the survivor is
    cancelled and a failure is re-raised.

    `extra` is for loops with the same lifetime as the gateway connection --
    the notification drain is one. They are cancelled with everything else
    when the process ends, and a failure in one ends the process rather than
    leaving it running with a feature silently dead.
    """
    tasks = {
        asyncio.ensure_future(work),
        asyncio.ensure_future(scope_loop(scope, state)),
        *(asyncio.ensure_future(c) for c in extra),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


@dataclass(frozen=True, slots=True)
class Process:
    """The bot process's object graph, assembled and ready to connect.

    Everything `main` runs, and nothing it has started: no gateway, no loops.
    Production and the end-to-end harness both get one from `assemble`, so
    the graph a test drives is the graph a deployment runs.
    """

    graph: BotGraph
    stack: AnswerStack
    scope: LiveScope
    conversations: Conversations
    facts: PersonalFactsService
    edges: Edges


async def assemble(settings: Settings, edges: Edges) -> Process:
    """Build the whole bot process over `edges`, stopping short of the network.

    The two checks that can refuse the deployment run in here -- the model's
    capabilities when `edges` was built, the embedding width inside
    `build_answer_stack` -- so a process that `assemble` returns is one whose
    model and corpus agree with its configuration.
    """
    stack = await build_answer_stack(settings, personal_facts=True, edges=edges)
    # Refreshed before the gateway identifies, so the first question is
    # answered from stored scope rather than the environment's. A failed read
    # here keeps the environment's scope, which is the scope in force at boot.
    scope = build_live_scope(settings, stack.engine, os.environ)
    await scope.refresh()
    # Over the same engine, so a turn is remembered through the pool the
    # question was answered through. Omitting this is the failure the old
    # in-memory store had: history recorded for a stage nobody wired.
    conversations = build_conversations(settings, stack.engine, edges.summary_chat)
    # Preferred name, email and language, over the same engine as memory.
    # Omitted, the whole feature is built, tested and reachable from nothing.
    facts = build_personal_facts(stack.engine)
    # `search` is what lets the withheld-evidence notice fire at all: it
    # probes the gap between what the asker may read and what the audience
    # may. Without it the notice is unreachable in the running process.
    graph = build_bot(
        settings,
        stack.answers,
        search=stack.search,
        confirmations=stack.federation.confirmations if stack.federation else None,
        # Over the engine the answer stack already holds, so a correction is
        # written through the same pool the obligation it corrects was read
        # through. Without this an ask can be extracted and never dismissed:
        # the correction path was built, tested and reachable from nothing.
        corrections=CorrectionService(PostgresAskStore(stack.engine), ask_policy(settings)),
        conversations=conversations,
        scope=scope,
        # `/index` and `/unindex`, writing through the same stored setting
        # this process and ingest refresh from. Without it both commands
        # answer that indexing is unavailable here.
        indexing=build_indexing_stores(stack.engine, scope),
        facts=facts,
        # "What did I miss in #x", over the same search backend and chat
        # handle the answer stack holds. Omitted, catch-up is built, tested
        # and reachable from nothing -- which is this project's failure mode.
        catchup=build_catch_up(settings, stack.search, stack.chat, edges.clock),
        # "What did Ana say about X last week", over the same backend and chat
        # handle, and on the same clock that decides what "last week" is.
        said_by=build_said_by(settings, stack.search, stack.chat, edges.clock),
        # The tracer `stack.answers` exports through, so catch-up and said-by
        # answers are traced like every other answer.
        tracer=stack.tracer,
        # Told once, on their first traced reply, that questions and answers
        # are recorded. None where tracing is off.
        tracing_notice=build_tracing_notice(settings, stack.engine, edges.clock),
        # The other end of the queue the ingest process fills. Omitted, the
        # notification tables are written by one process and read by none:
        # `/notifications` says it is unavailable, and nobody is ever told
        # that anything was asked of them.
        notifications=stack.engine,
        # The chain reads position alerts make, through the same transport as
        # every other outbound call, so a test that fakes the network fakes
        # this too.
        alert_transport=edges.http_transport,
        # When a new alert is first checked: one sweep after it is created, on
        # the clock the sweep itself runs on.
        clock=edges.clock,
        # Voice questions: the CDN download and the transcription through the
        # same transport as every other outbound call, the month on the same
        # clock. None unless VOICE_QUESTIONS_ENABLED.
        voice=build_voice_questions(settings, stack.engine, edges.http_transport, edges.clock),
        # What this deployment runs, for a bare mention. Without it the mention
        # describes only what every deployment has.
        capabilities=stack.capabilities,
    )
    return Process(
        graph=graph,
        stack=stack,
        scope=scope,
        conversations=conversations,
        facts=facts,
        edges=edges,
    )


async def main() -> None:
    log_setup.configure()
    settings = get_settings()
    state = HealthState()
    spawn(state, settings.health_port)

    process = await assemble(settings, Edges.production(settings))
    graph, scope = process.graph, process.scope
    state.details["indexing_scope"] = scope.status()
    client = graph.client

    original_on_ready = client.on_ready
    # Set before the drain starts: the permission re-check reads live guild
    # state, and a pass that runs before the gateway identifies sees every
    # member as able to read nothing.
    gateway_ready = asyncio.Event()

    async def on_ready() -> None:
        await original_on_ready()
        state.gateway_connected = True
        gateway_ready.set()

    client.on_ready = on_ready  # type: ignore[method-assign]

    indexed = scope.current()
    log.info("bot.starting", guild_id=settings.discord_guild_id, indexed=len(indexed))
    if not indexed:
        log.warning(
            "bot.no_indexed_channels",
            hint="add a channel in the admin console or set INDEXED_CHANNEL_IDS",
        )

    # The notification drain runs beside the gateway, in this process,
    # because this is the only process that can reach a person. Ingest queues;
    # this sends. Without this line the queue fills and nothing ever drains
    # it -- the eleventh time that failure would have shipped here.
    drains = (
        [
            notification_loop(
                graph.notifications, state, ready=gateway_ready, clock=process.edges.clock
            )
        ]
        if graph.notifications is not None
        else []
    )
    # Beside the drain, and for the same reason: this is the only process that
    # can both answer a question and reach a person. Without this line the
    # commands create tasks that nothing ever runs.
    if graph.tasks is not None:
        drains.append(
            scheduled_task_loop(
                graph.tasks,
                state,
                interval=settings.scheduled_sweep_seconds,
                ready=gateway_ready,
                clock=process.edges.clock,
            )
        )
    # Position alerts, beside the scheduled sweep for the same reason: this is
    # the process that can reach a person.
    if graph.alerts is not None:
        drains.append(
            alert_loop(
                graph.alerts,
                state,
                interval=settings.alert_sweep_seconds,
                ready=gateway_ready,
                clock=process.edges.clock,
            )
        )
    await run_beside_scope(
        client.start(settings.discord_token.get_secret_value()), scope, state, drains
    )


if __name__ == "__main__":
    asyncio.run(main())
