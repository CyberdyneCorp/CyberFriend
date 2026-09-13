"""Discord gateway ingestion process.

Runs exactly one replica. Two containers sharing a bot token both identify to
the gateway and ingest every message twice; Discord does not error, so the
duplication is silent. See docker-compose.yml.
"""

from __future__ import annotations

import asyncio

import structlog

from chatmemory import logging as log_setup
from chatmemory.config import get_settings
from chatmemory.health import HealthState, spawn

log = structlog.get_logger()


async def main() -> None:
    log_setup.configure()
    settings = get_settings()
    state = HealthState()
    spawn(state, settings.health_port)

    log.info(
        "ingest.starting",
        guild_id=settings.discord_guild_id,
        indexed_channels=len(settings.indexed_channel_ids),
        health_port=settings.health_port,
    )

    if not settings.indexed_channel_ids:
        # Indexing is opt-in: an empty scope means the corpus stays empty.
        # Surfaced loudly because "the bot is running but indexes nothing"
        # otherwise looks identical to "the bot is broken".
        log.warning("ingest.no_indexed_channels", hint="set INDEXED_CHANNEL_IDS")

    # Gateway client lands in task group 4.
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
