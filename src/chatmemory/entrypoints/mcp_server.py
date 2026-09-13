"""HTTP MCP retrieval interface.

The only service given a public domain. Every content-returning tool requires
an authenticated bearer token, and the token determines which person's view
the caller gets — the request cannot name a viewer. A leaked token therefore
exposes one person's view rather than the whole corpus.
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
    state = HealthState(gateway_connected=True)  # not gateway-dependent
    spawn(state, settings.mcp_port)

    log.info("mcp.starting", port=settings.mcp_port)

    # Tool surface lands in task group 8.
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
