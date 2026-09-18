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
remembers is built from guild state. `main` -> `build_bot` ->
`build_ask_service` -> `AskService(conversations=...)` is the whole chain, and
`tests/unit/test_memory_integration.py` reads it from this file down.

Personal facts follow the same route: `main` -> `build_personal_facts` over
the answer stack's engine -> `build_bot(facts=...)` -> `build_ask_service` ->
`AskService(facts=...)`, and `tests/unit/test_facts_behaviour.py` reads that
chain from this file down.

Catch-up summaries arrive the same way: `main` -> `build_catch_up` over the
answer stack's `search` and `chat` -> `build_bot(catchup=...)` ->
`build_ask_service` -> `AskService(catchup=...)`, and
`tests/unit/test_catchup_wiring.py` reads that chain from this file down. A
process that skips it answers "what did I miss in #x" by searching the corpus
for those words -- the behaviour before the feature, never a wider one.

Indexing scope is live here as it is in ingest. `main` builds a `LiveScope`
over the answer stack's engine, refreshes it before identifying, hands it to
`build_bot` -> `build_ask_service`, where both the ACL and the audience
resolver ask it on every question, and runs its refresh loop beside the
gateway. Handed the startup set instead, the bot kept answering from a channel
an operator removed and never from one they added, while ingest -- which did
read the store -- captured the new one. `test_scope_and_market_wiring` reads
that chain from this file down.

Notifications are the one thing this process does that nobody asked for, and
they are produced somewhere else. Ingest extracts obligations and writes queue
rows; this process holds the only connection a person can be messaged through,
so it drains them: `main` -> `build_bot(notifications=stack.engine)` ->
`build_delivery`, which attaches `/notifications` to the client and builds a
`NotificationDelivery` over a `DiscordAclResolver` on this client's live guild
state, and then `main` runs `notification_loop` beside the gateway.
`tests/unit/test_notifications_wiring.py` reads that chain from this file down.
The permission re-check is the resolver: a recipient's readable channels are
resolved at the moment of sending and bound into the query, so access revoked
between extraction and delivery removes the obligation from the message.

`/index` and `/unindex` act over that same `LiveScope`. `main` builds
`IndexingStores` over the answer stack's engine -- the configuration editor
the admin console writes through, the change record it reads, and the
message and document stores' existing channel purges -- and hands them to
`build_bot`, which attaches an `IndexingService` to the client with a
permission resolver and a notifier over this client's live guild cache. A
scope written there is re-read by this process at once and by ingest within
its refresh period. `test_chat_indexing_wiring` reads that chain.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory import logging as log_setup
from chatmemory.adapters.discord.acl import LiveGuild, PermissionCaches, _Guild
from chatmemory.adapters.discord.bot import (
    CyberFriendClient,
    DiscordChannelAccess,
    DiscordIndexNotifier,
    DiscordNotificationSender,
    _IndexingGuild,
)
from chatmemory.adapters.discord.gateway import attach_permission_listeners
from chatmemory.adapters.documents.store import PostgresDocumentStore
from chatmemory.adapters.store.admin_postgres import PostgresChangeRecord
from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.admin.audit import ChangeRecordStore
from chatmemory.app.ask import AskService
from chatmemory.app.asks.corrections import CorrectionService
from chatmemory.app.asks.obligations import discord_message_url
from chatmemory.app.authorization import ConfirmationLedger
from chatmemory.app.catchup import CatchUpService
from chatmemory.app.configuration import ConfigurationEditor
from chatmemory.app.conversation import Conversations
from chatmemory.app.facts import PersonalFactsService
from chatmemory.app.indexing import ChannelPurge, IndexingService
from chatmemory.app.notifications import NotificationDelivery
from chatmemory.app.scope import LiveScope, ScopeProvider
from chatmemory.composition import (
    ask_policy,
    build_answer_stack,
    build_ask_service,
    build_catch_up,
    build_conversations,
    build_live_scope,
    build_notification_delivery,
    build_notification_preferences,
    build_personal_facts,
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
    notifications: AsyncEngine | None = None,
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
    )
    if corrections is not None:
        # `/resolve` is registered either way, so without this every attempt to
        # close an ask is refused -- which is the safe half of the feature, and
        # the half a deployment that wires nothing should get.
        asks.attach_corrections(corrections)
    client = CyberFriendClient(asks, settings.discord_guild_id)
    attach_permission_listeners(client, caches)
    service = None
    if indexing is not None:
        # Without this `/index` and `/unindex` are registered and say they are
        # unavailable -- the safe half, and the half a deployment wiring
        # nothing gets.
        service = build_indexing_service(client, settings.discord_guild_id, indexing)
        client.attach_indexing(service)
    delivery = build_delivery(settings, client, provider, caches, notifications, scope)
    return BotGraph(
        client=client,
        asks=asks,
        caches=caches,
        indexing=service,
        notifications=delivery,
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


async def notification_loop(
    delivery: NotificationDelivery,
    state: HealthState,
    interval: float = NOTIFICATION_DRAIN_INTERVAL_SECONDS,
    ready: asyncio.Event | None = None,
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
            report = await delivery.deliver(datetime.now(UTC))
            state.details["notifications"] = {
                **report.as_dict(),
                "last_run_at": time.time(),
            }
        except Exception:
            # Nothing is lost: an unsent row stays pending, and the expiry
            # bound in the sweep stops that being for ever.
            log.exception("notifications.drain_failed")
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


async def main() -> None:
    log_setup.configure()
    settings = get_settings()
    state = HealthState()
    spawn(state, settings.health_port)

    stack = await build_answer_stack(settings, personal_facts=True)
    # Refreshed before the gateway identifies, so the first question is
    # answered from stored scope rather than the environment's. A failed read
    # here keeps the environment's scope, which is the scope in force at boot.
    scope = build_live_scope(settings, stack.engine, os.environ)
    await scope.refresh()
    state.details["indexing_scope"] = scope.status()
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
        # Over the same engine, so a turn is remembered through the pool the
        # question was answered through. Omitting this is the failure the old
        # in-memory store had: history recorded for a stage nobody wired.
        conversations=build_conversations(settings, stack.engine),
        scope=scope,
        # `/index` and `/unindex`, writing through the same stored setting
        # this process and ingest refresh from. Without it both commands
        # answer that indexing is unavailable here.
        indexing=build_indexing_stores(stack.engine, scope),
        # Preferred name, email and language, over the same engine as memory.
        # Omitted, the whole feature is built, tested and reachable from nothing.
        facts=build_personal_facts(stack.engine),
        # "What did I miss in #x", over the same search backend and chat
        # handle the answer stack holds. Omitted, catch-up is built, tested
        # and reachable from nothing -- which is this project's failure mode.
        catchup=build_catch_up(settings, stack.search, stack.chat),
        # The other end of the queue the ingest process fills. Omitted, the
        # notification tables are written by one process and read by none:
        # `/notifications` says it is unavailable, and nobody is ever told
        # that anything was asked of them.
        notifications=stack.engine,
    )
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
        [notification_loop(graph.notifications, state, ready=gateway_ready)]
        if graph.notifications is not None
        else []
    )
    await run_beside_scope(
        client.start(settings.discord_token.get_secret_value()), scope, state, drains
    )


if __name__ == "__main__":
    asyncio.run(main())
