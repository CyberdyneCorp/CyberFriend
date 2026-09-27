"""The `/me` guard: a user session, and nothing else, reaches a user route.

The mirror of `AdminAuthMiddleware`. That one reads only `__Host-cf_admin` or
a bearer token and never grants a `user` row; this one reads only
`__Host-cf_user` and grants nothing but `user` rows. So a user session is 401
on every console route and a console session is 401 on every user route, and
`tests/unit/test_user_area.py` asserts both.

*   Everything under `/me`, and any route whose row is `user`, is guarded
    here. Under `/me` a route without a `user` row is refused, so the static
    bundle's catch-all cannot open the prefix.
*   A request with an `Authorization` header is refused: no bearer token is a
    user credential.
*   Anything that is not a read needs the console's CSRF header and, when an
    `Origin` is sent, the console's own origin, as for cookie sessions there.
"""

from __future__ import annotations

from contextvars import ContextVar

import structlog
from starlette._utils import get_route_path
from starlette.types import ASGIApp, Receive, Scope, Send

from chatmemory.admin.access import Access, RouteAccess
from chatmemory.admin.auth import (
    CROSS_SITE,
    UNAUTHENTICATED,
    Denied,
    Unauthenticated,
    cookie_value,
    csrf_header_present,
    header_values,
    is_read,
    origin_allowed,
    refuse,
)
from chatmemory.admin.user.service import UserPrincipal, UserSignIn

log = structlog.get_logger()

USER_COOKIE = "__Host-cf_user"
"""The user area's session cookie: an opaque id, `HttpOnly; Secure; SameSite=Strict`."""

USER_PREFIX = "/me"

_NO_USER_ROW = Denied(403, b'{"error":"forbidden"}')

_current_user: ContextVar[UserPrincipal | None] = ContextVar(
    "chatmemory_user_principal", default=None
)


def current_user() -> UserPrincipal:
    """The account signed in on this `/me` request."""
    user = _current_user.get()
    if user is None:
        raise Unauthenticated("no user session in this request")
    return user


class UserAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        sign_in: UserSignIn | None,
        access: RouteAccess,
        public_origin: str | None,
        prefix: str = USER_PREFIX,
    ) -> None:
        self._app = app
        self._sign_in = sign_in
        self._access = access
        self._origin = public_origin
        self._prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        rule = self._access.rule_for(scope)
        if not self._guards(scope, rule.access):
            await self._app(scope, receive, send)
            return
        outcome = await self._authenticate(scope)
        if isinstance(outcome, UserPrincipal) and rule.access is not Access.USER:
            outcome = _NO_USER_ROW
        if isinstance(outcome, Denied):
            log.info("user.refused", path=scope.get("path"), status=outcome.status)
            await refuse(send, outcome.status, outcome.body)
            return
        handle = _current_user.set(outcome)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_user.reset(handle)

    def _guards(self, scope: Scope, access: Access | None) -> bool:
        path = get_route_path(scope)
        under = path == self._prefix or path.startswith(self._prefix + "/")
        return under or access is Access.USER

    async def _authenticate(self, scope: Scope) -> UserPrincipal | Denied:
        if header_values(scope, b"authorization"):
            return UNAUTHENTICATED
        session_id = cookie_value(scope, USER_COOKIE)
        if self._sign_in is None or session_id is None:
            return UNAUTHENTICATED
        if not is_read(scope) and not (
            csrf_header_present(scope) and origin_allowed(scope, self._origin)
        ):
            return CROSS_SITE
        return await self._sign_in.authenticate(session_id)
