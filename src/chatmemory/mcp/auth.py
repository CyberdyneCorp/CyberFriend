"""Authentication for the MCP surface: the credential decides the viewer.

Two things happen here and nowhere else:

1.  A bearer token is exchanged for a `PersonRef` through a server-side
    mapping, then for a `Viewer` through the ACL resolver.
2.  That viewer is placed in a context the tools read from.

Tools never receive the viewer as an argument, and the request has no field
that names one. An impersonation attempt therefore has nothing to aim at --
extra arguments, extra headers and forged bodies all reach code that simply
does not read them.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp, Receive, Scope, Send

from chatmemory.app.tokens import (
    IssuedToken,
    TokenRecord,
    TokenStore,
    generate_token,
    hash_token,
)
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.acl import AclResolver

log = structlog.get_logger()

PLATFORM = "discord"


class Unauthenticated(Exception):
    """No authenticated viewer is in scope.

    Raised by `current_viewer` rather than returning a permissive default:
    an unauthenticated read must be impossible to express, not merely
    unlikely to occur.
    """


_current_viewer: ContextVar[Viewer | None] = ContextVar("chatmemory_viewer", default=None)


def current_viewer() -> Viewer:
    """The viewer established by the credential on this request."""
    viewer = _current_viewer.get()
    if viewer is None:
        # Unreachable while the middleware guards every tool route; kept
        # because "the guard was removed" must fail closed rather than serve
        # the corpus to an anonymous caller.
        raise Unauthenticated("no authenticated viewer in this request")
    return viewer


def bind_viewer(viewer: Viewer) -> Token[Viewer | None]:
    """Bind a viewer to this context, returning a handle that undoes it.

    The single writer of the viewer context. In production only the
    middleware calls it, immediately after a credential has been verified;
    tests call it to exercise the tools without an HTTP round trip.
    """
    return _current_viewer.set(viewer)


def unbind_viewer(handle: Token[Viewer | None]) -> None:
    _current_viewer.reset(handle)


# --- storage -----------------------------------------------------------
#
# The token table is owned by this surface rather than by the ingestion
# schema, and is created idempotently at start-up so deploying the MCP
# service needs no migration coordination with the ingest process.

TOKEN_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mcp_token (
        token_hash        text PRIMARY KEY,
        platform          text NOT NULL,
        platform_user_id  bigint NOT NULL,
        label             text NOT NULL DEFAULT '',
        issued_at         timestamptz NOT NULL DEFAULT now(),
        revoked_at        timestamptz
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_mcp_token_person
        ON mcp_token (platform, platform_user_id) WHERE revoked_at IS NULL
    """,
)

LOOKUP_TOKEN = text("""
SELECT platform, platform_user_id FROM mcp_token
WHERE token_hash = :token_hash AND revoked_at IS NULL
""")

INSERT_TOKEN = text("""
INSERT INTO mcp_token (token_hash, platform, platform_user_id, label)
VALUES (:token_hash, :platform, :platform_user_id, :label)
""")

REVOKE_PERSON_TOKENS = text("""
UPDATE mcp_token SET revoked_at = now()
WHERE platform = :platform AND platform_user_id = :platform_user_id
  AND revoked_at IS NULL
""")

ACTIVE_TOKENS = text("""
SELECT token_hash, platform, platform_user_id, label, issued_at
FROM mcp_token WHERE revoked_at IS NULL ORDER BY issued_at
""")


async def ensure_token_schema(engine: AsyncEngine) -> None:
    """Create the token table if it is absent. Safe to run concurrently."""
    async with engine.begin() as conn:
        for statement in TOKEN_SCHEMA:
            await conn.execute(text(statement))


class PostgresTokenStore:
    """The token -> person mapping, stored as hashes.

    Every write is scoped to one person, so rotating or revoking a single
    person's credential never touches anyone else's -- the property that
    decides whether rotation actually happens when it should.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def person_for_token(self, token: str) -> PersonRef | None:
        async with self._engine.connect() as conn:
            row = await conn.execute(LOOKUP_TOKEN, {"token_hash": hash_token(token)})
            found = row.mappings().first()
        if found is None:
            # Unknown and revoked are the same answer on purpose: the caller
            # learns that this credential does not work, and nothing else.
            return None
        return PersonRef(str(found["platform"]), int(found["platform_user_id"]))

    async def issue(self, person: PersonRef, label: str = "") -> IssuedToken:
        token = generate_token()
        digest = hash_token(token)
        async with self._engine.begin() as conn:
            await conn.execute(
                INSERT_TOKEN,
                {
                    "token_hash": digest,
                    "platform": person.platform,
                    "platform_user_id": person.platform_user_id,
                    "label": label,
                },
            )
        return IssuedToken(person=person, token=token, token_hash=digest, label=label)

    async def revoke(self, person: PersonRef) -> int:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                REVOKE_PERSON_TOKENS,
                {
                    "platform": person.platform,
                    "platform_user_id": person.platform_user_id,
                },
            )
            return result.rowcount or 0

    async def rotate(self, person: PersonRef, label: str = "") -> IssuedToken:
        """Revoke then issue, in one transaction.

        One transaction so a crash between the two cannot leave the person
        with no working credential and no record of why.
        """
        token = generate_token()
        digest = hash_token(token)
        async with self._engine.begin() as conn:
            await conn.execute(
                REVOKE_PERSON_TOKENS,
                {
                    "platform": person.platform,
                    "platform_user_id": person.platform_user_id,
                },
            )
            await conn.execute(
                INSERT_TOKEN,
                {
                    "token_hash": digest,
                    "platform": person.platform,
                    "platform_user_id": person.platform_user_id,
                    "label": label,
                },
            )
        return IssuedToken(person=person, token=token, token_hash=digest, label=label)

    async def active_tokens(self) -> Sequence[TokenRecord]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(ACTIVE_TOKENS)
            return [
                TokenRecord(
                    person=PersonRef(str(r["platform"]), int(r["platform_user_id"])),
                    token_hash=str(r["token_hash"]),
                    label=str(r["label"]),
                    issued_at=cast(datetime, r["issued_at"]),
                )
                for r in rows.mappings()
            ]


# --- request authentication --------------------------------------------


@dataclass(frozen=True, slots=True)
class Authenticator:
    """Turns a credential into a viewer. The only path to a viewer there is."""

    tokens: TokenStore
    acl: AclResolver

    async def viewer_for_token(self, token: str) -> Viewer | None:
        person = await self.tokens.person_for_token(token)
        if person is None:
            return None
        # Permissions are resolved now, not stored with the token: a revoked
        # role takes effect on the next call with no re-issue and no reindex.
        return await self.acl.resolve_viewer(person)


def bearer_token(header_value: str | None) -> str | None:
    """Extract a bearer credential from an Authorization header value."""
    if not header_value:
        return None
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


_UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'


class BearerAuthMiddleware:
    """Rejects unauthenticated calls, and binds the viewer for the rest.

    Runs at the ASGI layer so an unauthenticated request never reaches the
    tool dispatcher at all. The response is byte-identical for a missing, a
    malformed, an unknown and a revoked credential, so the endpoint cannot be
    used to enumerate who holds a token.
    """

    def __init__(
        self, app: ASGIApp, authenticator: Authenticator, protected_prefix: str = "/mcp"
    ) -> None:
        self._app = app
        self._authenticator = authenticator
        self._prefix = protected_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(self._prefix):
            # Health and readiness stay open: an orchestrator probing them
            # holds no credential, and they expose no message content.
            await self._app(scope, receive, send)
            return

        raw = cast("list[tuple[bytes, bytes]]", scope["headers"])
        presented = [v for k, v in raw if k.lower() == b"authorization"]
        # Exactly one, or none. A dict of headers would keep the last of a
        # repeated Authorization, so a request carrying two credentials would
        # be authorised as whichever one we happened to read -- and a proxy in
        # front that reads the first would disagree with us about who called.
        # Ambiguity about the caller's identity is refused, not resolved.
        token = bearer_token(presented[0].decode("latin-1")) if len(presented) == 1 else None
        viewer = await self._authenticator.viewer_for_token(token) if token else None

        if viewer is None:
            log.info("mcp.unauthenticated", path=scope.get("path"))
            await _unauthorized(send)
            return

        # Note what is *not* read here: no header, query parameter or body
        # field can name a viewer, because only the token is consulted.
        handle = bind_viewer(viewer)
        try:
            await self._app(scope, receive, send)
        finally:
            unbind_viewer(handle)


async def _unauthorized(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="chatmemory"'),
                (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
