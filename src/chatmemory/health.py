"""Health reporting.

Coolify health checks are HTTP, and the Discord gateway client would otherwise
expose no port at all. Beyond liveness this reports the things that fail
*silently*: a gateway that disconnected and never reconnected, a backfill that
stalled, an embedding backlog that is growing faster than it drains. A process
that is up while doing none of its work is the failure mode worth catching.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


@dataclass
class HealthState:
    """Mutable snapshot the running components update as they work."""

    gateway_connected: bool = False
    backfill_lag_seconds: float | None = None
    embedding_backlog: int | None = None
    last_message_ingested_at: float | None = None
    details: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "gateway_connected": self.gateway_connected,
            "backfill_lag_seconds": self.backfill_lag_seconds,
            "embedding_backlog": self.embedding_backlog,
            "last_message_ingested_at": self.last_message_ingested_at,
            **self.details,
        }


ReadinessCheck = Callable[[], Awaitable[bool]]


def build_app(state: HealthState, readiness: ReadinessCheck | None = None) -> Starlette:
    async def health(_: Request) -> JSONResponse:
        # Liveness: the process is running and serving. Deliberately cheap and
        # dependency-free, so a database blip cannot trigger a restart loop.
        return JSONResponse({"status": "ok", **state.as_dict()})

    async def ready(_: Request) -> JSONResponse:
        ok = await readiness() if readiness is not None else state.gateway_connected
        payload: dict[str, object] = {"status": "ready" if ok else "not_ready"}
        payload.update(state.as_dict())
        return JSONResponse(payload, status_code=200 if ok else 503)

    return Starlette(routes=[Route("/health", health), Route("/ready", ready)])


async def serve(app: Starlette, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    await uvicorn.Server(config).serve()


def spawn(
    state: HealthState, port: int, readiness: ReadinessCheck | None = None
) -> asyncio.Task[None]:
    """Run the health server alongside the component it reports on."""
    return asyncio.create_task(serve(build_app(state, readiness), port))
