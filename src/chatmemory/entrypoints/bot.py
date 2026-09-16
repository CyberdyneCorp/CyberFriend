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

Indexing scope is live here as it is in ingest. `main` builds a `LiveScope`
over the answer stack's engine, refreshes it before identifying, hands it to
`build_bot` -> `build_ask_service`, where both the ACL and the audience
resolver ask it on every question, and runs its refresh loop beside the
gateway. Handed the startup set instead, the bot kept answering from a channel
an operator removed and never from one they added, while ingest -- which did
read the store -- captured the new one. `test_scope_and_market_wiring` reads
that chain from this file down.

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
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory import logging as log_setup
from chatmemory.adapters.discord.acl import LiveGuild, PermissionCaches, _Guild
from chatmemory.adapters.discord.bot import (
    CyberFriendClient,
    DiscordChannelAccess,
    DiscordIndexNotifier,
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
from chatmemory.app.authorization import ConfirmationLedger
from chatmemory.app.configuration import ConfigurationEditor
from chatmemory.app.conversation import Conversations
from chatmemory.app.indexing import ChannelPurge, IndexingService
from chatmemory.app.scope import LiveScope, ScopeProvider
from chatmemory.composition import (
    ask_policy,
    build_answer_stack,
    build_ask_service,
    build_conversations,
    build_live_scope,
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


def build_bot(
    settings: Settings,
    answers: AnswerService,
    search: SearchBackend | None = None,
    confirmations: ConfirmationLedger | None = None,
    corrections: CorrectionService | None = None,
    conversations: Conversations | None = None,
    scope: ScopeProvider | None = None,
    indexing: IndexingStores | None = None,
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
    return BotGraph(client=client, asks=asks, caches=caches, indexing=service)


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


async def run_beside_scope(
    work: Coroutine[Any, Any, None], scope: LiveScope, state: HealthState
) -> None:
    """Run the process's work with the scope refresh loop, ending with whichever ends.

    Not `gather`: when the gateway returns, the process must exit rather than
    sit refreshing a scope nothing uses; when the loop dies, the process must
    exit rather than serve a frozen scope. Either way the survivor is
    cancelled and a failure is re-raised.
    """
    tasks = {asyncio.ensure_future(work), asyncio.ensure_future(scope_loop(scope, state))}
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

    stack = await build_answer_stack(settings)
    # Refreshed before the gateway identifies, so the first question is
    # answered from stored scope rather than the environment's. A failed read
    # here keeps the environment's scope, which is the scope in force at boot.
    scope = build_live_scope(settings, stack.engine, os.environ)
    await scope.refresh()
    state.details["indexing_scope"] = scope.status()
    # `search` is what lets the withheld-evidence notice fire at all: it
    # probes the gap between what the asker may read and what the audience
    # may. Without it the notice is unreachable in the running process.
    client = build_bot(
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
    ).client

    original_on_ready = client.on_ready

    async def on_ready() -> None:
        await original_on_ready()
        state.gateway_connected = True

    client.on_ready = on_ready  # type: ignore[method-assign]

    indexed = scope.current()
    log.info("bot.starting", guild_id=settings.discord_guild_id, indexed=len(indexed))
    if not indexed:
        log.warning(
            "bot.no_indexed_channels",
            hint="add a channel in the admin console or set INDEXED_CHANNEL_IDS",
        )

    await run_beside_scope(client.start(settings.discord_token.get_secret_value()), scope, state)


if __name__ == "__main__":
    asyncio.run(main())
