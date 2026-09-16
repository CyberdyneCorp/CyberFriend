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
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

import structlog

from chatmemory import logging as log_setup
from chatmemory.adapters.discord.acl import LiveGuild, PermissionCaches, _Guild
from chatmemory.adapters.discord.bot import CyberFriendClient
from chatmemory.adapters.discord.gateway import attach_permission_listeners
from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.app.ask import AskService
from chatmemory.app.asks.corrections import CorrectionService
from chatmemory.app.authorization import ConfirmationLedger
from chatmemory.composition import ask_policy, build_answer_stack, build_ask_service
from chatmemory.config import Settings, get_settings
from chatmemory.health import HealthState, spawn
from chatmemory.ports.answers import AnswerService
from chatmemory.ports.store import SearchBackend

log = structlog.get_logger()


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


def build_bot(
    settings: Settings,
    answers: AnswerService,
    search: SearchBackend | None = None,
    confirmations: ConfirmationLedger | None = None,
    corrections: CorrectionService | None = None,
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
    )
    if corrections is not None:
        # `/resolve` is registered either way, so without this every attempt to
        # close an ask is refused -- which is the safe half of the feature, and
        # the half a deployment that wires nothing should get.
        asks.attach_corrections(corrections)
    client = CyberFriendClient(asks, settings.discord_guild_id)
    attach_permission_listeners(client, caches)
    return BotGraph(client=client, asks=asks, caches=caches)


async def main() -> None:
    log_setup.configure()
    settings = get_settings()
    state = HealthState()
    spawn(state, settings.health_port)

    stack = await build_answer_stack(settings)
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
    ).client

    original_on_ready = client.on_ready

    async def on_ready() -> None:
        await original_on_ready()
        state.gateway_connected = True

    client.on_ready = on_ready  # type: ignore[method-assign]

    indexed = settings.indexed_channel_ids
    log.info("bot.starting", guild_id=settings.discord_guild_id, indexed=len(indexed))
    if not indexed:
        log.warning("bot.no_indexed_channels", hint="set INDEXED_CHANNEL_IDS")

    await client.start(settings.discord_token.get_secret_value())


if __name__ == "__main__":
    asyncio.run(main())
