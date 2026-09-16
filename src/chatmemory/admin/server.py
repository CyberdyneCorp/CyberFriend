"""The console as one ASGI application: the API, and the interface it serves.

One container, deliberately. A separate static host for the interface would
need its own domain, its own certificate and a CORS policy wide enough for a
browser to cross between them -- and a CORS policy on the highest-privilege
surface in the system is a thing somebody will later widen for a good reason.
Served from the same origin, there is no cross-origin request to permit, so
this file adds no CORS middleware at all and none is missing.

What is authenticated and what is not:

*   Everything under `/api` requires a credential, which names the operator.
    The middleware runs at the ASGI layer, so an unauthenticated request never
    reaches a handler that could change anything, and missing, malformed,
    unknown and revoked all produce byte-identical refusals.
*   `/health` and `/ready` are open. A probe holds no credential and neither
    route reveals configuration.
*   The interface bundle is open. It is code, not configuration: it contains
    no credential, holds nothing in local storage, and is useless without a
    token typed into it.

The last route is a refusal. Anything under `/api` that no handler claimed
gets a written "this console configures the agent and returns no message,
document or ask content" rather than a bare 404 -- because the question that
lands there is usually somebody's client trying `/api/messages`, and the
answer to that is a boundary, not a typo.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from chatmemory.admin.auth import AdminAuthenticator, AdminAuthMiddleware, OperatorTokens
from chatmemory.admin.handlers import (
    changes,
    channels,
    federation,
    optouts,
    settings,
    status,
    tokens,
)
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.handlers.support import Refused, refusal

log = structlog.get_logger()

API_PREFIX = "/api"

NO_CORPUS = (
    "this console configures the agent; it returns no message, document or ask "
    "content, and there is no endpoint that would"
)
"""The refusal for any unclaimed path under /api.

Written as a rule rather than as "not found" because that is what it is. The
console is not a window into the corpus, and making it one would create a
second path to private channels that bypasses every viewer check in the
system.
"""

MISSING_CONSOLE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>CyberFriend admin</title></head>
<body style="font-family: system-ui; margin: 4rem auto; max-width: 40rem">
<h1>The API is running; the interface bundle is not here.</h1>
<p>The console's static files were not found. Build them and point
<code>ADMIN_CONSOLE_DIR</code> at the output, or use the API directly with a
bearer token issued by <code>python -m chatmemory.admin.issue_token</code>.</p>
</body></html>"""
"""Served at / when the built interface is absent.

A page saying so, rather than a 404. The failure this replaces is a deploy
where the bundle did not get copied in and every request returned "not found",
which reads as a broken API rather than as a missing build step.
"""


def api_routes(services: AdminServices) -> list[Route]:
    """Every configuring route, in the order they are matched.

    Assembled from the handler modules rather than listed here, so adding a
    screen means adding a module and one line -- and so this file cannot
    quietly acquire a route that reaches the corpus.
    """
    return [
        *status.routes(services),
        *settings.routes(services),
        *federation.routes(services),
        *channels.routes(services),
        *optouts.routes(services),
        *tokens.routes(services),
        *changes.routes(services),
    ]


def build_app(
    services: AdminServices,
    tokens_store: OperatorTokens,
    console_dir: Path | None = None,
) -> Starlette:
    """The console application: ports in, one ASGI app out.

    Takes ports rather than an engine so the same wiring runs in tests against
    fakes and in production against Postgres -- and so that a test which
    forgets to pass something fails to construct the app rather than serving a
    screen that silently does nothing.
    """

    async def health(_: Request) -> JSONResponse:
        # Liveness: dependency-free, so a database blip cannot restart the one
        # surface an operator would use to find out about it.
        return JSONResponse({"status": "ok"})

    async def ready(_: Request) -> JSONResponse:
        snapshot = await services.status.snapshot()
        ok = snapshot.database_reachable
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "database_reachable": ok},
            status_code=200 if ok else 503,
        )

    async def no_corpus(_: Request) -> JSONResponse:
        return JSONResponse({"error": NO_CORPUS}, status_code=404)

    routes: list[Route | Mount] = [
        Route("/health", health, methods=["GET"], name="health"),
        Route("/ready", ready, methods=["GET"], name="ready"),
        *api_routes(services),
        # Last under /api, so it claims only what no handler did. It also
        # answers a wrong method with this refusal rather than a 405, which
        # tells a caller nothing about which verbs exist.
        Route(
            f"{API_PREFIX}/{{rest:path}}",
            no_corpus,
            # Every verb, explicitly. A route left to default to GET would let
            # a POST fall through to a 405 assembled from the routes above,
            # which is a list of what exists -- and the audit would answer
            # "method not allowed" to a caller asking to rewrite it, rather
            # than "there is no such thing here".
            methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            name="no_corpus",
        ),
        *_console(console_dir),
    ]

    app = Starlette(routes=routes, exception_handlers={Refused: refusal})
    # Added after the routes and around the whole app: every path under /api
    # is authenticated before a handler sees the request, and the handlers
    # read the operator from the context the middleware bound.
    app.add_middleware(
        AdminAuthMiddleware,
        authenticator=AdminAuthenticator(tokens_store),
        protected_prefix=API_PREFIX,
    )
    return app


def _console(console_dir: Path | None) -> Sequence[Route | Mount]:
    """Serve the built interface, or say plainly that it is not there."""
    if console_dir is not None and console_dir.is_dir():
        log.info("admin.console_bundle", directory=str(console_dir))
        return [Mount("/", app=StaticFiles(directory=console_dir, html=True), name="console")]
    log.warning(
        "admin.console_bundle_missing",
        directory=str(console_dir) if console_dir else None,
        hint="the API is serving; build the interface and set ADMIN_CONSOLE_DIR",
    )

    async def placeholder(_: Request) -> Response:
        return HTMLResponse(MISSING_CONSOLE)

    return [Route("/", placeholder, methods=["GET"], name="console")]
