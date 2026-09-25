"""Admin authentication: the credential names the principal, and nothing else does.

Static bearer tokens were chosen over an OAuth flow, and the honest cost of
that choice is recorded in the change's design note. What is *not* conceded is
attribution: a token is issued to one named operator and stored as a hash, so
the change record can say "Ana enabled this" rather than "the token did it".
A shared credential would make the audit decorative exactly where it matters
most -- on the surface that decides what the agent may reach.

Three properties are load-bearing here:

*   **Only the credential decides.** There is no header, query parameter or
    body field that selects an operator, so a caller-supplied identity has
    nothing to aim at. It is the same rule the MCP surface follows, for the
    same reason: an asserted identity is not a fact.
*   **One refusal, four causes.** Missing, malformed, unknown and revoked
    credentials produce byte-identical responses. A refusal that says which
    one you got is an oracle for enumerating who holds a token.
*   **Revocation is per operator.** Every write is keyed on one operator, so
    withdrawing one person's access leaves everybody else working -- which is
    what decides whether revocation actually happens when someone leaves.

*   **The credential also decides the role.** A `cfa_` token is admin while
    no identity provider is configured, exactly as before roles existed, and
    operator (read-only) once one is: admin rights then come only from the
    provider's role, which is named, managed and revocable there. Which role
    each route needs is `access.py`'s question, not this module's.

*   **A bearer header wins.** A request with `Authorization` is authenticated
    by it alone and its `Cookie` header is stripped before anything else sees
    it, so a CyberdyneAuth session cookie (`__Host-cf_admin`, see
    `admin.oidc`) is never read, refreshed or used as a fallback beside one.
    A request authenticated by the cookie instead must carry the console's
    CSRF header on anything that is not a read, and every non-read with an
    `Origin` must come from the console's own origin.

Header parsing is deliberately not imported from `mcp.auth`. The two surfaces
authenticate different principals against different tables, and sharing the
code would mean a change made for one silently changes the other.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, cast

import structlog

# From `_utils` because that is where the router's own copy comes from:
# `starlette.routing` and `starlette.staticfiles` both import it from here,
# and re-implementing the root_path rule would let the guard's idea of the
# path drift from the router's on an upgrade -- which is the whole bug this
# call exists to prevent. If a future Starlette moves it, the import fails
# loudly at startup rather than quietly matching the wrong path.
from starlette._utils import get_route_path
from starlette.types import ASGIApp, Receive, Scope, Send

from chatmemory.app.tokens import TOKEN_ENTROPY_BYTES, hash_token

if TYPE_CHECKING:
    from chatmemory.admin.access import RouteAccess, Rule

log = structlog.get_logger()

ADMIN_TOKEN_PREFIX = "cfa_"
"""Distinct from the MCP prefix so a credential pasted into the wrong surface,
or found in a log, is identifiable as the console one. Carries no entropy."""

#: Operator names are constrained so that they can appear verbatim in the
#: change record and in log lines without escaping, and -- specifically --
#: so that they cannot contain ':'. The audit reserves that character for
#: non-operator actors (see `audit.SHELL_ACTOR`), and a name able to contain
#: it could forge one.
OPERATOR_NAME = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")


class Unauthenticated(Exception):
    """No authenticated principal is in scope.

    Raised by `current_operator` rather than returning a permissive default:
    an unattributed configuration change must be impossible to express, not
    merely unlikely to occur.
    """


OIDC_ACTOR_PREFIX = "oidc:"
"""The change record's actor for a signed-in person: `oidc:<sub>`.

Operator names cannot contain ':', so no token holder can be recorded as one.
"""


@dataclass(frozen=True, slots=True)
class Operator:
    """The named holder of a console token, and the actor the change record names.

    A name, and nothing else. What the holder may do is not a property of the
    name but of the request: the authenticator turns the token into a
    `Principal`, whose roles depend on how the console is configured.
    """

    name: str

    def __post_init__(self) -> None:
        if not OPERATOR_NAME.match(self.name):
            raise ValueError(
                f"operator name {self.name!r} must match {OPERATOR_NAME.pattern}"
            )

    def __str__(self) -> str:
        return self.name

    @property
    def display(self) -> str | None:
        """Nothing beyond the name: a token holder is shown as their name."""
        return None


@dataclass(frozen=True, slots=True)
class Actor:
    """Who the change record names for this request, and what to show beside it.

    `name` is an operator name for a token and `oidc:<sub>` for a signed-in
    person; `display` is the person's email, kept in its own column.
    """

    name: str
    display: str | None = None


class Role(StrEnum):
    """A console role. Admin implies operator."""

    OPERATOR = "operator"
    ADMIN = "admin"


Via = Literal["oidc", "token"]
"""How the principal authenticated: a CyberdyneAuth session or a `cfa_` token."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting on this request, and with which roles.

    `subject` is the stable identity (the operator name for a token, the OIDC
    `sub` for a session) and `display` is what a screen shows. Built only by
    the authenticator from a verified credential; nothing in a request can
    name one.
    """

    subject: str
    display: str
    roles: frozenset[Role]
    via: Via

    def has(self, role: Role) -> bool:
        if role in self.roles:
            return True
        return role is Role.OPERATOR and Role.ADMIN in self.roles

    @classmethod
    def for_token(cls, operator: Operator, *, oidc_configured: bool) -> Principal:
        """A `cfa_` token's principal: admin until an issuer is configured.

        Once sign-in through the identity provider is configured, a token is
        operator only. A leaked token then reads counts and configuration and
        changes nothing; rolling back is unsetting the issuer.
        """
        roles = {Role.OPERATOR} if oidc_configured else {Role.OPERATOR, Role.ADMIN}
        return cls(
            subject=operator.name, display=operator.name, roles=frozenset(roles), via="token"
        )


@dataclass(frozen=True, slots=True)
class IssuedOperatorToken:
    """A newly minted console credential.

    `token` is plaintext and exists only in the return value of `issue`. It is
    never persisted, so a lost credential is rotated rather than recovered.
    """

    operator: Operator
    token: str
    token_hash: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class OperatorTokenRecord:
    """A stored credential, without the credential."""

    operator: Operator
    token_hash: str
    label: str
    issued_at: datetime
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


def generate_admin_token() -> str:
    """A fresh console credential. Returned once, at issue, never recoverable."""
    return ADMIN_TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


class OperatorTokens(Protocol):
    """The credential -> operator mapping.

    Read the method list as the guarantee. There is no method that takes a
    name and returns a credential, and none that takes a credential and
    returns anything but the one operator it was issued to, so impersonation
    has no entry point here either.
    """

    async def operator_for_token(self, token: str) -> Operator | None:
        """The operator this credential acts as, or None if it does not work.

        Returns None identically for an unknown credential and a revoked one:
        the caller learns that it does not work, and nothing else.
        """
        ...

    async def issue(self, operator: Operator, label: str = "") -> IssuedOperatorToken:
        """Mint a credential for one named operator."""
        ...

    async def revoke(self, operator: Operator) -> int:
        """Revoke every live credential of one operator. Returns how many.

        Scoped to the operator on purpose: revoking one person must never be
        an outage for the rest, or it will be deferred until it is too late.
        """
        ...

    async def live_credentials(self, operator: Operator) -> int:
        """How many working credentials one operator currently holds.

        Exists so the change record can carry a before and an after for a
        grant or a withdrawal rather than only the fact that one happened.
        """
        ...

    async def active_tokens(self) -> Sequence[OperatorTokenRecord]:
        """Live credentials, for an operator review. Hashes only."""
        ...


class InMemoryOperatorTokens:
    """An operator token store for tests and single-process development.

    Stores hashes like the real one, so a test that accidentally asserts on
    stored plaintext fails here too rather than only in production.
    """

    def __init__(self) -> None:
        self._records: dict[str, OperatorTokenRecord] = {}

    async def operator_for_token(self, token: str) -> Operator | None:
        record = self._records.get(hash_token(token))
        if record is None or not record.is_active:
            return None
        return record.operator

    async def issue(self, operator: Operator, label: str = "") -> IssuedOperatorToken:
        token = generate_admin_token()
        digest = hash_token(token)
        self._records[digest] = OperatorTokenRecord(
            operator=operator,
            token_hash=digest,
            label=label,
            issued_at=datetime.now().astimezone(),
        )
        return IssuedOperatorToken(
            operator=operator, token=token, token_hash=digest, label=label
        )

    async def revoke(self, operator: Operator) -> int:
        now = datetime.now().astimezone()
        revoked = 0
        for digest, record in list(self._records.items()):
            if record.operator == operator and record.is_active:
                self._records[digest] = OperatorTokenRecord(
                    operator=record.operator,
                    token_hash=record.token_hash,
                    label=record.label,
                    issued_at=record.issued_at,
                    revoked_at=now,
                )
                revoked += 1
        return revoked

    async def live_credentials(self, operator: Operator) -> int:
        return sum(
            1 for r in self._records.values() if r.operator == operator and r.is_active
        )

    async def active_tokens(self) -> Sequence[OperatorTokenRecord]:
        return tuple(r for r in self._records.values() if r.is_active)


# --- the principal in scope --------------------------------------------

_current_principal: ContextVar[Principal | None] = ContextVar(
    "chatmemory_admin_principal", default=None
)


def current_principal() -> Principal:
    """The principal established by the credential on this request."""
    principal = _current_principal.get()
    if principal is None:
        # Unreachable while the middleware guards every console route; kept
        # because "the guard was removed" must fail closed rather than let a
        # change be applied with nobody's name against it.
        raise Unauthenticated("no authenticated principal in this request")
    return principal


def current_operator() -> Operator:
    """The token holder acting on this request, as the change record names them.

    Only a token principal has an `Operator`. A session principal is recorded
    as `oidc:<sub>`, which is not an operator name by construction, so asking
    for one here refuses rather than inventing it.
    """
    principal = current_principal()
    if principal.via != "token":
        raise Unauthenticated("this request was not made with an operator token")
    return Operator(principal.subject)


def current_actor() -> Actor:
    """The actor the change record names for this request's principal."""
    principal = current_principal()
    if principal.via == "token":
        return Actor(Operator(principal.subject).name)
    display = principal.display if principal.display != principal.subject else None
    return Actor(OIDC_ACTOR_PREFIX + principal.subject, display)


def bind_principal(principal: Principal) -> Token[Principal | None]:
    """Bind a principal to this context, returning a handle that undoes it.

    The single writer of the principal context. In production only the
    middleware calls it, immediately after a credential has been verified;
    tests call it to exercise handlers without an HTTP round trip.
    """
    return _current_principal.set(principal)


def unbind_principal(handle: Token[Principal | None]) -> None:
    _current_principal.reset(handle)


# --- request authentication --------------------------------------------

SESSION_COOKIE = "__Host-cf_admin"
"""The console's session cookie: an opaque id, `HttpOnly; Secure; SameSite=Strict`."""

LOGIN_COOKIE = "__Host-cf_login"
"""The pre-sign-in cookie that binds a sign-in to the browser that started it."""

CSRF_HEADER = "x-cyberfriend-console"
"""Required, with value `1`, on every cookie-authenticated request that is not a read.

A cross-site form cannot set it, and a cross-site `fetch` with it needs a CORS
preflight, which this server never grants.
"""

READ_METHODS = frozenset({"GET", "HEAD"})

_UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'
_REQUIRES_ADMIN_BODY = b'{"error":"requires admin"}'
_NO_RULE_BODY = b'{"error":"forbidden"}'


@dataclass(frozen=True, slots=True)
class Denied:
    """A refusal decided while authenticating, with the status and body to send."""

    status: int
    body: bytes


UNAUTHENTICATED = Denied(401, _UNAUTHORIZED_BODY)
NO_CONSOLE_ACCESS = Denied(403, b'{"error":"no console access"}')
CROSS_SITE = Denied(403, b'{"error":"cross-site request refused"}')


class SessionAuthenticator(Protocol):
    """Turns a session cookie into a principal (see `admin.oidc.service`)."""

    async def authenticate(self, session_id: str) -> Principal | Denied:
        """The session's principal, refreshed if due, or why it is refused."""
        ...


@dataclass(frozen=True, slots=True)
class AdminAuthenticator:
    """Turns a credential into a principal. The only path to one there is.

    Deliberately thinner than the MCP authenticator: there is no ACL lookup
    behind it, because the console grants no view of the corpus that a
    permission set could narrow. `oidc_configured` is whether CyberdyneAuth
    sign-in is configured, which is what downscopes tokens to operator;
    `sessions` is the sign-in itself, and `public_origin` the only `Origin` a
    non-read may come from.
    """

    tokens: OperatorTokens
    oidc_configured: bool = False
    sessions: SessionAuthenticator | None = None
    public_origin: str | None = None

    async def principal_for_token(self, token: str) -> Principal | None:
        operator = await self.tokens.operator_for_token(token)
        if operator is None:
            return None
        return Principal.for_token(operator, oidc_configured=self.oidc_configured)


def bearer_token(header_value: str | None) -> str | None:
    """Extract a bearer credential from an Authorization header value."""
    if not header_value:
        return None
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def header_values(scope: Scope, name: bytes) -> list[bytes]:
    raw = cast("list[tuple[bytes, bytes]]", scope["headers"])
    return [v for k, v in raw if k.lower() == name]


def cookie_value(scope: Scope, name: str) -> str | None:
    """The one value of cookie `name`, or None when it is absent or repeated.

    Repeated is refused rather than resolved, for the same reason as a
    repeated Authorization header: whichever copy we read, something in front
    of us might read the other.
    """
    found: list[str] = []
    for header in header_values(scope, b"cookie"):
        for part in header.decode("latin-1").split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == name:
                found.append(value.strip())
    return found[0] if len(found) == 1 and found[0] else None


def without_cookies(scope: Scope) -> Scope:
    """The same request with its `Cookie` header removed."""
    raw = cast("list[tuple[bytes, bytes]]", scope["headers"])
    return {**scope, "headers": [(k, v) for k, v in raw if k.lower() != b"cookie"]}


def origin_allowed(scope: Scope, public_origin: str | None) -> bool:
    """An `Origin`, when sent, must be the console's own. Scripts send none."""
    origins = header_values(scope, b"origin")
    if not origins:
        return True
    if len(origins) != 1:
        return False
    # Sign-in off: no public origin is configured, and bearer requests behave
    # exactly as before sign-in existed.
    return public_origin is None or origins[0].decode("latin-1") == public_origin


def csrf_header_present(scope: Scope) -> bool:
    values = header_values(scope, CSRF_HEADER.encode())
    return len(values) == 1 and values[0] == b"1"


def is_read(scope: Scope) -> bool:
    return str(scope.get("method", "GET")).upper() in READ_METHODS


class AdminAuthMiddleware:
    """Authenticates, decides the route's role, and binds the principal.

    Runs at the ASGI layer so a request never reaches a handler it may not
    use. The 401 is byte-identical for a missing, a malformed, an unknown and
    a revoked credential -- a refusal that distinguished them would let anyone
    holding one dead token learn which names still have live ones. A
    principal below the route's role gets 403, and so does any route the
    table has no row for (deny by default).
    """

    def __init__(
        self,
        app: ASGIApp,
        authenticator: AdminAuthenticator,
        access: RouteAccess,
        protected_prefix: str = "/api",
    ) -> None:
        self._app = app
        self._authenticator = authenticator
        self._access = access
        self._prefix = protected_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        rule = self._access.rule_for(scope)
        if self._is_open(scope, rule):
            await self._app(scope, receive, send)
            return

        outcome, scope = await self._authenticate(scope)
        if isinstance(outcome, Denied):
            log.info("admin.refused", path=scope.get("path"), status=outcome.status)
            await _refuse(send, outcome.status, outcome.body)
            return

        if not rule.permits(outcome):
            log.info(
                "admin.forbidden",
                path=scope.get("path"),
                principal=outcome.subject,
                required=rule.access.value if rule.access else None,
            )
            await _refuse(send, 403, _REQUIRES_ADMIN_BODY if rule.access else _NO_RULE_BODY)
            return

        # Note what is *not* read here: no header, query parameter or body
        # field can name a principal, because only the credential is
        # consulted. `receive` is passed through untouched, so the body has
        # not even been read at the point identity is settled.
        handle = bind_principal(outcome)
        try:
            await self._app(scope, receive, send)
        finally:
            unbind_principal(handle)

    def _is_open(self, scope: Scope, rule: Rule) -> bool:
        """Outside the protected prefix: a public row, or a path no route claims.

        `get_route_path`, never `scope["path"]`: the router matches routes on
        the path with `root_path` stripped, and a guard that matched on the
        raw path would disagree with it under any non-empty root_path -- a
        console mounted at /console/ would show this middleware
        "/console/api/settings", miss the prefix and wave the request through
        to the handler the router then resolves for "/api/settings". The
        guard has to answer the same question the router will.
        """
        # Under the prefix a request is authenticated first, whatever claims
        # it: the bundle's Mount at "/" also matches "/api" and "/apix", and
        # a public row reached that way must not open the prefix.
        if get_route_path(scope).startswith(self._prefix):
            return False
        # A public row, or nothing claims it (the router answers 404 and no
        # handler runs).
        return rule.is_public or not rule.matched

    async def _authenticate(self, scope: Scope) -> tuple[Principal | Denied, Scope]:
        """The principal, and the scope the handler will see.

        With an Authorization header the bearer alone decides, and the scope
        loses its cookies: the session is not read, refreshed or used as a
        fallback, and an invalid bearer is a 401 whatever cookie came with it.
        """
        presented = header_values(scope, b"authorization")
        if not presented:
            return await self._by_session(scope), scope
        scope = without_cookies(scope)
        principal = await self._by_bearer(presented)
        if principal is None:
            return UNAUTHENTICATED, scope
        if not is_read(scope) and not origin_allowed(scope, self._authenticator.public_origin):
            return CROSS_SITE, scope
        return principal, scope

    async def _by_bearer(self, presented: list[bytes]) -> Principal | None:
        # Exactly one. A dict of headers would keep the last of a repeated
        # Authorization, so a request carrying two credentials would be
        # attributed to whichever one we happened to read -- and a proxy in
        # front that reads the first would disagree with us about who acted.
        # On the surface where attribution is the product, ambiguity about
        # the actor is refused rather than resolved.
        if len(presented) != 1:
            return None
        token = bearer_token(presented[0].decode("latin-1"))
        if token is None:
            return None
        return await self._authenticator.principal_for_token(token)

    async def _by_session(self, scope: Scope) -> Principal | Denied:
        sessions = self._authenticator.sessions
        session_id = cookie_value(scope, SESSION_COOKIE) if sessions else None
        if sessions is None or session_id is None:
            return UNAUTHENTICATED
        # Before the session is touched: a cross-site request must not even
        # cause a refresh.
        if not is_read(scope) and not (
            csrf_header_present(scope)
            and origin_allowed(scope, self._authenticator.public_origin)
        ):
            return CROSS_SITE
        return await sessions.authenticate(session_id)


async def _refuse(send: Send, status: int, body: bytes) -> None:
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if status == 401:
        headers.insert(1, (b"www-authenticate", b'Bearer realm="chatmemory-admin"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
