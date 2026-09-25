"""The console as one ASGI application: the API, and the interface it serves.

One container, deliberately. A separate static host for the interface would
need its own domain, its own certificate and a CORS policy wide enough for a
browser to cross between them -- and a CORS policy on the highest-privilege
surface in the system is a thing somebody will later widen for a good reason.
Served from the same origin, there is no cross-origin request to permit, so
this file adds no CORS middleware at all and none is missing.

What is authenticated and what is not:

*   Everything under `/api` requires a credential, which names the principal.
    The middleware runs at the ASGI layer, so an unauthenticated request never
    reaches a handler that could change anything, and missing, malformed,
    unknown and revoked all produce byte-identical refusals.
*   Which role each route needs is `ROUTE_ACCESS`, one explicit row per
    mounted `(method, path)`. A route with no row is refused before its
    handler runs; every write is admin; reads are operator.
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

from collections.abc import Mapping, Sequence
from pathlib import Path

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from chatmemory.admin.access import Access, RouteAccess, RouteKey
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


_PUBLIC = Access.PUBLIC
_OPERATOR = Access.OPERATOR
_ADMIN = Access.ADMIN

NO_CORPUS_PATH = f"{API_PREFIX}/{{rest:path}}"

ROUTE_ACCESS: Mapping[RouteKey, Access] = {
    # Probes and the interface bundle hold no configuration.
    ("GET", "/health"): _PUBLIC,
    ("GET", "/ready"): _PUBLIC,
    ("GET", "/"): _PUBLIC,  # the "bundle missing" placeholder
    ("GET", "/{path:path}"): _PUBLIC,  # the static bundle, mounted at /
    # Status and the change record.
    ("GET", "/api/status"): _OPERATOR,
    ("GET", "/api/audit"): _OPERATOR,
    # Settings.
    ("GET", "/api/settings"): _OPERATOR,
    ("PUT", "/api/settings/{key}"): _ADMIN,
    # Federation: adding a server or allowing a tool widens what the agent reaches.
    ("GET", "/api/federation/servers"): _OPERATOR,
    ("POST", "/api/federation/servers"): _ADMIN,
    ("DELETE", "/api/federation/servers/{name}"): _ADMIN,
    ("GET", "/api/federation/allowlist"): _OPERATOR,
    ("POST", "/api/federation/allowlist"): _ADMIN,
    ("DELETE", "/api/federation/allowlist/{server}/{tool}"): _ADMIN,
    # Channels.
    ("GET", "/api/channels"): _OPERATOR,
    ("POST", "/api/channels"): _ADMIN,
    ("DELETE", "/api/channels/{id}"): _ADMIN,
    # Opt-outs: adding one purges a person's messages.
    ("GET", "/api/optouts"): _OPERATOR,
    ("POST", "/api/optouts"): _ADMIN,
    ("DELETE", "/api/optouts/{platform}/{id}"): _ADMIN,
    # MCP credentials: review and revoke only.
    ("GET", "/api/tokens"): _OPERATOR,
    ("DELETE", "/api/tokens/{id}"): _ADMIN,
    # The refusal for anything unclaimed under /api. Its non-read verbs are
    # admin like every other write, so an operator's POST is a 403 rather than
    # a tour of which paths exist.
    ("GET", NO_CORPUS_PATH): _OPERATOR,
    ("POST", NO_CORPUS_PATH): _ADMIN,
    ("PUT", NO_CORPUS_PATH): _ADMIN,
    ("PATCH", NO_CORPUS_PATH): _ADMIN,
    ("DELETE", NO_CORPUS_PATH): _ADMIN,
    ("OPTIONS", NO_CORPUS_PATH): _ADMIN,
}
"""The access every mounted `(method, path)` requires. Deny by default.

Kept beside `api_routes()` so a new route and its row are one diff. There is
no per-method default: a route missing here is refused with 403 for everyone,
and `tests/unit/test_admin_roles.py` walks every mounted route and fails on a
missing row, on a write below admin, and on a row for a route that no longer
exists.
"""


def build_app(
    services: AdminServices,
    tokens_store: OperatorTokens,
    console_dir: Path | None = None,
    *,
    oidc_configured: bool = False,
    route_access: Mapping[RouteKey, Access] = ROUTE_ACCESS,
) -> Starlette:
    """The console application: ports in, one ASGI app out.

    Takes ports rather than an engine so the same wiring runs in tests against
    fakes and in production against Postgres -- and so that a test which
    forgets to pass something fails to construct the app rather than serving a
    screen that silently does nothing.

    `oidc_configured` is whether an identity provider issuer is set; it
    downscopes `cfa_` tokens from admin to operator. `route_access` is
    injectable so a test can prove that a route without a row is refused.
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
            NO_CORPUS_PATH,
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
    # is authenticated and checked against its row before a handler sees the
    # request, and the handlers read the principal the middleware bound. The
    # table is resolved against the router's own route list, so the guard and
    # the router always agree on which route a request is.
    app.add_middleware(
        AdminAuthMiddleware,
        authenticator=AdminAuthenticator(tokens_store, oidc_configured=oidc_configured),
        access=RouteAccess(route_access, app.router.routes),
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
