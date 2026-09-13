"""Discord bot process.

Runs the conversational surface. Ingestion is a separate process; this one
only answers questions.

Nothing is wired here: the object graph comes from `composition`, which is
also where the two checks that can refuse the deployment live. That ordering
matters -- a model that cannot honour a response schema, or an embedding
model whose vectors are the wrong width, stops this process before it
identifies to the gateway rather than after people start asking it things.
"""

from __future__ import annotations

import asyncio
from typing import cast

import structlog

from chatmemory import logging as log_setup
from chatmemory.adapters.discord.acl import _Guild
from chatmemory.adapters.discord.bot import CyberFriendClient
from chatmemory.composition import build_answer_stack, build_ask_service
from chatmemory.config import get_settings
from chatmemory.health import HealthState, spawn

log = structlog.get_logger()


async def main() -> None:
    log_setup.configure()
    settings = get_settings()
    state = HealthState()
    spawn(state, settings.health_port)

    # Resolvers read live guild state, which does not exist until the
    # gateway connects. They take a provider and treat a cold cache as an
    # empty guild, so permission resolution fails closed during startup.
    client: CyberFriendClient | None = None

    def guild() -> _Guild | None:
        if client is None:
            return None
        # discord.Guild satisfies _Guild in practice, but not structurally:
        # permissions_for accepts Member | Role rather than any _Member, so
        # the protocol is contravariantly incompatible. The cast is the
        # adapter boundary -- the protocol exists so the core stays testable
        # against fakes, not to re-describe discord.py's type hierarchy.
        return cast(_Guild | None, client.get_guild(settings.discord_guild_id))

    stack = await build_answer_stack(settings)
    asks = build_ask_service(settings, guild, stack.answers)

    client = CyberFriendClient(asks, settings.discord_guild_id)

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
