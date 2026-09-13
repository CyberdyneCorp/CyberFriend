"""Discord bot process.

Runs the conversational surface. Ingestion is a separate process; this one
only answers questions.
"""

from __future__ import annotations

import asyncio
from typing import cast

import structlog

from chatmemory import logging as log_setup
from chatmemory.adapters.discord.acl import (
    DiscordAclResolver,
    DiscordAudienceResolver,
    _Guild,
)
from chatmemory.adapters.discord.bot import CyberFriendClient
from chatmemory.app.ask import AskService, StubAnswerService
from chatmemory.app.conversation import ConversationStore
from chatmemory.app.limits import RateLimiter
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

    indexed = settings.indexed_channel_ids
    asks = AskService(
        acl=DiscordAclResolver(guild, indexed),
        audiences=DiscordAudienceResolver(guild, indexed),
        answers=StubAnswerService(),
        limiter=RateLimiter(),
        conversations=ConversationStore(),
    )

    client = CyberFriendClient(asks, settings.discord_guild_id)

    original_on_ready = client.on_ready

    async def on_ready() -> None:
        await original_on_ready()
        state.gateway_connected = True

    client.on_ready = on_ready  # type: ignore[method-assign]

    log.info("bot.starting", guild_id=settings.discord_guild_id, indexed=len(indexed))
    if not indexed:
        log.warning("bot.no_indexed_channels", hint="set INDEXED_CHANNEL_IDS")

    await client.start(settings.discord_token.get_secret_value())


if __name__ == "__main__":
    asyncio.run(main())
