"""GET /api/status -- is the agent doing its work, and how far behind is it.

The three numbers an operator actually needs are the ones that fail quietly:
the newest message archived (a gateway can be connected and ingesting
nothing), the embedding backlog (a queue that stops draining leaves recent
conversation unsearchable while every process reports itself healthy), and how
many channels are still waiting for a window rebuild.

None of it is content. The answer to "is ingestion working" is a count and a
timestamp, and a console that quoted the newest message to prove it would have
become a second route into private channels.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chatmemory.admin.handlers.queries import IngestionProgress, StatusSnapshot
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.handlers.support import moment
from chatmemory.app.configuration import (
    INDEXED_CHANNEL_IDS,
    REFRESH_INTERVAL_SECONDS,
)
from chatmemory.ports.configuration import SettingSource


def routes(services: AdminServices) -> list[Route]:
    async def status(_: Request) -> JSONResponse:
        snapshot = await services.status.snapshot()
        return JSONResponse(
            {
                # "ok" only when the console could actually ask. A status page
                # that says "ok" from cached zeros is worse than no page.
                "status": "ok" if snapshot.database_reachable else "unavailable",
                "database_reachable": snapshot.database_reachable,
                "ingestion": _ingestion(snapshot),
                "embedding_backlog": snapshot.embedding_backlog,
                "indexed_channels": snapshot.indexed_channels,
                "configuration": _configuration(services),
            },
            # 200 either way: this endpoint reports a failure, it is not one.
            # A 503 here would make an orchestrator restart the console for
            # the crime of correctly saying the database is down.
            status_code=200,
        )

    return [Route("/api/status", status, methods=["GET"], name="status")]


def _ingestion(snapshot: StatusSnapshot) -> dict[str, Any] | None:
    progress: IngestionProgress | None = snapshot.ingestion
    if progress is None:
        return None
    return {
        "messages": progress.messages,
        "windows": progress.windows,
        # The figure that catches a silently dead gateway: it stops moving
        # while everything else looks healthy.
        "newest_message_at": moment(progress.newest_message_at),
        "channels_with_cursor": progress.channels_with_cursor,
        "channels_backfilled": progress.channels_backfilled,
        "channels_pending_rebuild": progress.channels_pending_rebuild,
    }


def _configuration(services: AdminServices) -> dict[str, Any]:
    """How much of the configuration in force came from the database.

    Reported because it answers the question an operator asks immediately
    after editing something: "has anything actually picked this up". The
    refresh interval is the honest upper bound on how long that takes.
    """
    current = services.configuration.current
    stored = sum(1 for v in current.values.values() if v.source is SettingSource.DATABASE)
    return {
        "settings_from_database": stored,
        "settings_total": len(current.values),
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
        "channels_in_scope": len(current.get(INDEXED_CHANNEL_IDS)),
    }
