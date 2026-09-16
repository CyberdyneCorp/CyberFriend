"""Admin authentication: the credential names the operator, and nothing else does.

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
from typing import Protocol, cast

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
    """No authenticated operator is in scope.

    Raised by `current_operator` rather than returning a permissive default:
    an unattributed configuration change must be impossible to express, not
    merely unlikely to occur.
    """


@dataclass(frozen=True, slots=True)
class Operator:
    """A named person who may change configuration.

    A name, and nothing else. No roles, no permissions, no grants: every
    operator may change everything the console exposes. That is a real
    limitation and it is deliberate -- a permission model nobody maintains
    reads as a boundary while enforcing nothing, and the console's actual
    boundary is that it holds no credentials and cannot reach the corpus.
    """

    name: str

    def __post_init__(self) -> None:
        if not OPERATOR_NAME.match(self.name):
            raise ValueError(
                f"operator name {self.name!r} must match {OPERATOR_NAME.pattern}"
            )

    def __str__(self) -> str:
        return self.name


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


# --- the operator in scope ---------------------------------------------

_current_operator: ContextVar[Operator | None] = ContextVar(
    "chatmemory_operator", default=None
)


def current_operator() -> Operator:
    """The operator established by the credential on this request."""
    operator = _current_operator.get()
    if operator is None:
        # Unreachable while the middleware guards every console route; kept
        # because "the guard was removed" must fail closed rather than let a
        # change be applied with nobody's name against it.
        raise Unauthenticated("no authenticated operator in this request")
    return operator


def bind_operator(operator: Operator) -> Token[Operator | None]:
    """Bind an operator to this context, returning a handle that undoes it.

    The single writer of the operator context. In production only the
    middleware calls it, immediately after a credential has been verified;
    tests call it to exercise handlers without an HTTP round trip.
    """
    return _current_operator.set(operator)


def unbind_operator(handle: Token[Operator | None]) -> None:
    _current_operator.reset(handle)


# --- request authentication --------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminAuthenticator:
    """Turns a credential into an operator. The only path to one there is.

    Deliberately thinner than the MCP authenticator: there is no ACL lookup
    behind it, because the console grants no view of the corpus that a
    permission set could narrow.
    """

    tokens: OperatorTokens

    async def operator_for_token(self, token: str) -> Operator | None:
        return await self.tokens.operator_for_token(token)


def bearer_token(header_value: str | None) -> str | None:
    """Extract a bearer credential from an Authorization header value."""
    if not header_value:
        return None
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


_UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'


class AdminAuthMiddleware:
    """Rejects unauthenticated calls, and binds the operator for the rest.

    Runs at the ASGI layer so an unauthenticated request never reaches a
    handler that could change anything. The response is byte-identical for a
    missing, a malformed, an unknown and a revoked credential -- a refusal
    that distinguished them would let anyone holding one dead token learn
    which names still have live ones.
    """

    def __init__(
        self,
        app: ASGIApp,
        authenticator: AdminAuthenticator,
        protected_prefix: str = "/api",
    ) -> None:
        self._app = app
        self._authenticator = authenticator
        self._prefix = protected_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # `get_route_path`, never `scope["path"]`: the router matches routes on
        # the path with `root_path` stripped, and a guard that matched on the
        # raw path would disagree with it under any non-empty root_path -- a
        # console mounted at /console/ (a shape the deployment notes advertise)
        # would show this middleware "/console/api/settings", miss the prefix
        # and wave the request through to the handler the router then resolves
        # for "/api/settings". The guard has to answer the same question the
        # router will, or it is guarding a different application.
        if scope["type"] != "http" or not get_route_path(scope).startswith(self._prefix):
            # Health, readiness and the static console bundle stay open: a
            # probe holds no credential, and the bundle is code, not
            # configuration. Everything that reads or writes configuration
            # lives under the protected prefix.
            await self._app(scope, receive, send)
            return

        operator = await self._operator_for(scope)
        if operator is None:
            log.info("admin.unauthenticated", path=scope.get("path"))
            await _unauthorized(send)
            return

        # Note what is *not* read here: no header, query parameter or body
        # field can name an operator, because only the credential is
        # consulted. `receive` is passed through untouched, so the body has
        # not even been read at the point identity is settled.
        handle = bind_operator(operator)
        try:
            await self._app(scope, receive, send)
        finally:
            unbind_operator(handle)

    async def _operator_for(self, scope: Scope) -> Operator | None:
        raw = cast("list[tuple[bytes, bytes]]", scope["headers"])
        presented = [v for k, v in raw if k.lower() == b"authorization"]
        # Exactly one, or none. A dict of headers would keep the last of a
        # repeated Authorization, so a request carrying two credentials would
        # be attributed to whichever one we happened to read -- and a proxy in
        # front that reads the first would disagree with us about who acted.
        # On the surface where attribution is the product, ambiguity about
        # the actor is refused rather than resolved.
        if len(presented) != 1:
            return None
        token = bearer_token(presented[0].decode("latin-1"))
        if token is None:
            return None
        return await self._authenticator.operator_for_token(token)


async def _unauthorized(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="chatmemory-admin"'),
                (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
