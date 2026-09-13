"""The ASGI application: MCP over HTTP, plus health and readiness.

Health lives on the same port as the tools because that is the only port the
orchestrator is given, and it stays unauthenticated because a probe holds no
credential and the endpoint exposes no message content.

Connecting a Claude Code session:

    {
      "mcpServers": {
        "chatmemory": {
          "type": "http",
          "url": "https://chatmemory.example.com/mcp",
          "headers": { "Authorization": "Bearer cfm_..." }
        }
      }
    }

The token is the whole identity. There is no `viewer` field to add, and
adding one to the stanza changes nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from chatmemory.health import HealthState, ReadinessCheck
from chatmemory.mcp.auth import Authenticator, BearerAuthMiddleware
from chatmemory.mcp.tools import build_server
from chatmemory.ports.store import SearchBackend

log = structlog.get_logger()

MCP_PATH = "/mcp"


def _transport_security(allowed_hosts: Sequence[str] | None) -> TransportSecuritySettings:
    """Host-header validation, when we have been told what to expect.

    DNS-rebinding protection defends a *locally bound* MCP server from a page
    in the user's browser. This one is published on a domain behind a reverse
    proxy, where the Host header is whatever that proxy forwards and the
    actual access control is the bearer token. So it is opt-in: an allowlist
    guessed wrong here fails every request with a 421 that looks nothing like
    a configuration error.
    """
    if not allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=list(allowed_hosts)
    )


def build_app(
    search: SearchBackend,
    authenticator: Authenticator,
    guild_id: int,
    state: HealthState | None = None,
    readiness: ReadinessCheck | None = None,
    allowed_hosts: Sequence[str] | None = None,
) -> Starlette:
    """Build the served application.

    Takes the retrieval port and an authenticator rather than an engine, so
    the same wiring runs in tests against fakes and in production against
    Postgres and the live guild cache.
    """
    health_state = state if state is not None else HealthState(gateway_connected=True)
    server: MCPServer = build_server(search, guild_id)

    async def health(_: Request) -> JSONResponse:
        # Liveness only: dependency-free, so a database blip cannot restart us.
        return JSONResponse({"status": "ok", **health_state.as_dict()})

    async def ready(_: Request) -> JSONResponse:
        ok = await readiness() if readiness is not None else True
        payload: dict[str, object] = {"status": "ready" if ok else "not_ready"}
        payload.update(health_state.as_dict())
        return JSONResponse(payload, status_code=200 if ok else 503)

    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        # Stateless and JSON: no per-session server state to pin a caller to
        # one replica, and no SSE stream for a proxy to buffer.
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(allowed_hosts),
    )
    # Unauthenticated on purpose: an orchestrator's probe holds no credential,
    # and neither route can reach message content.
    app.router.add_route("/health", health, methods=["GET"], name="health")
    app.router.add_route("/ready", ready, methods=["GET"], name="ready")
    # Added here rather than around the app so the lifespan that starts the
    # session manager still reaches Starlette untouched.
    app.add_middleware(
        BearerAuthMiddleware, authenticator=authenticator, protected_prefix=MCP_PATH
    )
    return app
