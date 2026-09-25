"""Which role each console route needs, decided in one place and failing closed.

The console's destructive calls -- purging a person's messages, enabling a
state-changing federated tool -- sit on the same API as the read-only
screens. Letting each handler check its own role would put the boundary in as
many places as there are handlers, and the one that forgot would be the one
that mattered. So every mounted `(method, path)` has an explicit row in one
table (`server.ROUTE_ACCESS`), and this module answers two questions from it:
which row a request lands on, and whether a principal satisfies it.

Three rules carry the weight:

*   **No row, no access.** A route the table does not name is refused before
    its handler runs, whatever its method. There is no per-method default, so
    adding a POST without a row cannot quietly make it an operator route.
*   **The row is the router's row.** The request is matched against the
    application's own route list, in the router's order and with the router's
    `root_path` rule, so the guard cannot be asking about a different route
    from the one that will serve the request.
*   **HEAD is GET.** Starlette serves HEAD with the GET handler of a route, so
    it is looked up under the GET row rather than given a row of its own that
    could disagree with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from starlette.routing import BaseRoute, Match, Mount, Route
from starlette.types import Scope

from chatmemory.admin.auth import Principal, Role

RouteKey = tuple[str, str]
"""`(method, path template)`, e.g. `("DELETE", "/api/tokens/{id}")`."""


class Access(StrEnum):
    """What a route requires of the caller."""

    #: No credential: probes, the static bundle, and (later) `/auth/*`.
    PUBLIC = "public"
    #: The user area's own session (`/me/*`). No console principal meets it.
    USER = "user"
    #: Read-only console access: counts and configuration.
    OPERATOR = "operator"
    #: Anything that changes something.
    ADMIN = "admin"
    #: Personal content. Admin, and signed in as a person rather than a token.
    ADMIN_OIDC = "admin_oidc"


#: Every access level a non-read route may have. A test holds the table to it.
WRITE_ACCESS = frozenset({Access.ADMIN, Access.ADMIN_OIDC})


def permits(principal: Principal, access: Access | None) -> bool:
    """Whether a console principal may use a route with this access level.

    `None` -- a route without a row -- permits nobody. `USER` is not a console
    level: the user area authenticates with its own session, which never
    satisfies a console route, and the reverse holds too.
    """
    if access is Access.PUBLIC:
        return True
    if access is Access.OPERATOR:
        return principal.has(Role.OPERATOR)
    if access is Access.ADMIN:
        return principal.has(Role.ADMIN)
    if access is Access.ADMIN_OIDC:
        return principal.has(Role.ADMIN) and principal.via == "oidc"
    return False


def route_keys(route: BaseRoute) -> tuple[RouteKey, ...]:
    """The table keys a mounted route occupies.

    A `Route` occupies one key per method it accepts, HEAD folded into GET. A
    `Mount` (the static bundle) serves reads only, so it occupies GET on its
    prefix; any other method there has no row and is refused.
    """
    if isinstance(route, Route):
        methods = sorted(route.methods or ())
        return tuple((m, route.path) for m in methods if m != "HEAD")
    if isinstance(route, Mount):
        return (("GET", mount_template(route)),)
    return ()


def mount_template(mount: Mount) -> str:
    return f"{mount.path}/{{path:path}}"


@dataclass(frozen=True, slots=True)
class Rule:
    """The row a request landed on.

    `matched` is False when no route claims the path at all, which the router
    will answer with a 404; `access` is None when a route claims it but the
    table has no row for it, which is refused.
    """

    matched: bool
    access: Access | None

    @property
    def is_public(self) -> bool:
        return self.access is Access.PUBLIC

    def permits(self, principal: Principal) -> bool:
        return permits(principal, self.access)


class RouteAccess:
    """The route table, bound to the routes it describes."""

    def __init__(self, table: Mapping[RouteKey, Access], routes: Sequence[BaseRoute]) -> None:
        self._table = dict(table)
        self._routes = routes

    def rule_for(self, scope: Scope) -> Rule:
        route = self._route_for(scope)
        if route is None:
            return Rule(matched=False, access=None)
        return Rule(matched=True, access=self._table.get(_key(route, scope)))

    def _route_for(self, scope: Scope) -> BaseRoute | None:
        # The router's own order: the first full match serves the request,
        # and a path-only match (wrong method) is what it falls back to.
        partial: BaseRoute | None = None
        for route in self._routes:
            match, _ = route.matches(scope)
            if match is Match.FULL:
                return route
            if match is Match.PARTIAL and partial is None:
                partial = route
        return partial


def _key(route: BaseRoute, scope: Scope) -> RouteKey:
    method = str(scope.get("method", "GET")).upper()
    method = "GET" if method == "HEAD" else method
    if isinstance(route, Mount):
        return (method, mount_template(route))
    return (method, getattr(route, "path", ""))
