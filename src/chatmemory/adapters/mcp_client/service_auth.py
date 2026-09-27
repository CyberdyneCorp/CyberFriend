"""A service identity for one federated server: CyberdyneAuth client credentials.

Some servers (CyberWealth's MCP server first) refuse any request without a
bearer. For a server given a credential, a token is minted with the
`client_credentials` grant, cached until shortly before it expires, and sent
on that server's MCP requests -- and nowhere else. The token lives on an
`httpx2.Auth` attached to an HTTP client built for that one server, never on a
default header or a shared client, so a second server, a cross-origin redirect
(which the SDK does not follow) or the token endpoint itself never sees it.

Neither the client secret nor a token is logged: `repr` hides the secret, and
a refused grant is reported by its OAuth `error` code alone.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

import httpx
import httpx2
import structlog
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from chatmemory.adapters.mcp_client.client import SessionFactory
from chatmemory.adapters.mcp_client.config import ConfigurationError, ServerConfig
from chatmemory.adapters.mcp_client.session import (
    ToolSession,
    default_session_factory,
    open_session,
)

log = structlog.get_logger()

ENV_PREFIX = "FEDERATION_AUTH_"
DISCOVERY_PATH = "/.well-known/openid-configuration"
EXPIRY_MARGIN_SECONDS = 60.0
DEFAULT_LIFETIME_SECONDS = 300.0
TOKEN_TIMEOUT_SECONDS = 10.0
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Longest first, so `_CLIENT_SECRET` is not read as a server named `X_CLIENT`.
_FIELDS = ("client_secret", "client_id", "issuer", "audience", "scope")
_REQUIRED = ("issuer", "client_id", "client_secret")


class ServiceTokenUnavailable(Exception):
    """No token could be obtained. Degrades the server like any transport error."""


@dataclass(frozen=True, slots=True)
class ServiceCredential:
    """One server's CyberdyneAuth client. The secret never appears in `repr`."""

    server: str
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    audience: str | None = None
    scope: str | None = None


def env_name(server: str) -> str:
    """`cyber-wealth` -> `CYBER_WEALTH`: the `<NAME>` in `FEDERATION_AUTH_<NAME>_*`."""
    return server.upper().replace("-", "_")


def _split_key(key: str) -> tuple[str, str] | None:
    """`FEDERATION_AUTH_CYBERWEALTH_CLIENT_ID` -> (`CYBERWEALTH`, `client_id`)."""
    rest = key[len(ENV_PREFIX):]
    for name in _FIELDS:
        suffix = "_" + name.upper()
        if rest.endswith(suffix) and len(rest) > len(suffix):
            return rest[: -len(suffix)], name
    return None


def _is_secure(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" or (
        parts.scheme == "http" and (parts.hostname or "") in _LOCAL_HOSTS
    )


def _gather(
    servers: Mapping[str, ServerConfig], environ: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    by_env = {env_name(name): name for name in servers}
    found: dict[str, dict[str, str]] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX) or not value.strip():
            continue
        split = _split_key(key)
        if split is None or split[0] not in by_env:
            raise ConfigurationError(
                f"{key} names no configured federation server; the form is "
                f"{ENV_PREFIX}<SERVER>_ISSUER|_CLIENT_ID|_CLIENT_SECRET|_AUDIENCE|_SCOPE"
            )
        found.setdefault(by_env[split[0]], {})[split[1]] = value.strip()
    return found


def _credential(server: ServerConfig, values: Mapping[str, str]) -> ServiceCredential:
    missing = [f for f in _REQUIRED if f not in values]
    if missing:
        names = ", ".join(f"{ENV_PREFIX}{env_name(server.name)}_{f.upper()}" for f in missing)
        raise ConfigurationError(f"service credential for {server.name!r} is missing {names}")
    if not _is_secure(values["issuer"]):
        raise ConfigurationError(f"service credential for {server.name!r}: issuer is not https")
    if not _is_secure(server.target):
        raise ConfigurationError(
            f"server {server.name!r} has a service credential but its target is not https"
        )
    return ServiceCredential(
        server=server.name,
        issuer=values["issuer"],
        client_id=values["client_id"],
        client_secret=values["client_secret"],
        audience=values.get("audience"),
        scope=values.get("scope"),
    )


def service_credentials(
    servers: Iterable[ServerConfig], environ: Mapping[str, str]
) -> dict[str, ServiceCredential]:
    """Every `FEDERATION_AUTH_*` credential, keyed by server name.

    Raises `ConfigurationError` for a key naming no configured server, a
    half-written credential, or a bearer that would travel over plain HTTP.
    """
    by_name = {s.name: s for s in servers}
    return {
        name: _credential(by_name[name], values)
        for name, values in _gather(by_name, environ).items()
    }


class ServiceTokenSource:
    """Mints and caches one server's bearer. One fetch at a time."""

    def __init__(
        self,
        credential: ServiceCredential,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credential = credential
        self._transport = transport
        self._monotonic = monotonic
        self._token_endpoint: str | None = None
        self._auth_methods: tuple[str, ...] = ()
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def server(self) -> str:
        return self._credential.server

    async def bearer(self) -> str:
        """A token valid for at least the expiry margin, minted if needed."""
        async with self._lock:
            if self._token is None or self._monotonic() >= self._expires_at:
                self._token, lifetime = await self._mint()
                self._expires_at = self._monotonic() + max(
                    0.0, lifetime - EXPIRY_MARGIN_SECONDS
                )
            return self._token

    def invalidate(self, token: str) -> None:
        """Drop `token` if it is still the cached one (the server refused it)."""
        if self._token == token:
            self._token = None

    async def _mint(self) -> tuple[str, float]:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=TOKEN_TIMEOUT_SECONDS
        ) as http:
            try:
                endpoint = await self._endpoint(http)
                data, headers = self._grant()
                response = await http.post(endpoint, data=data, headers=headers)
            except httpx.HTTPError as exc:
                raise ServiceTokenUnavailable(f"{self.server}: issuer unreachable") from exc
        body = _json(response)
        if response.status_code != 200:
            error = body.get("error")
            code = error if isinstance(error, str) else f"http {response.status_code}"
            log.warning("federation.service_token.refused", server=self.server, error=code)
            raise ServiceTokenUnavailable(f"{self.server}: token refused ({code})")
        return _token_of(body, self.server)

    async def _endpoint(self, http: httpx.AsyncClient) -> str:
        if self._token_endpoint is not None:
            return self._token_endpoint
        issuer = self._credential.issuer.rstrip("/")
        doc = _json(await http.get(issuer + DISCOVERY_PATH))
        named, endpoint = doc.get("issuer"), doc.get("token_endpoint")
        if not isinstance(named, str) or named.rstrip("/") != issuer:
            raise ServiceTokenUnavailable(f"{self.server}: discovery names a different issuer")
        if not isinstance(endpoint, str) or not _is_secure(endpoint):
            raise ServiceTokenUnavailable(f"{self.server}: discovery has no usable token endpoint")
        methods = doc.get("token_endpoint_auth_methods_supported")
        self._auth_methods = (
            tuple(m for m in methods if isinstance(m, str)) if isinstance(methods, list) else ()
        )
        self._token_endpoint = endpoint
        return endpoint

    def _grant(self) -> tuple[dict[str, str], dict[str, str]]:
        """The form and headers: client_secret_basic unless only _post is offered."""
        cred = self._credential
        form = {"grant_type": "client_credentials"}
        if cred.audience:
            form["audience"] = cred.audience
        if cred.scope:
            form["scope"] = cred.scope
        methods = self._auth_methods
        if methods and "client_secret_basic" not in methods and "client_secret_post" in methods:
            return {**form, "client_id": cred.client_id, "client_secret": cred.client_secret}, {}
        # RFC 6749 2.3.1: form-encode each half before the Basic encoding.
        pair = f"{quote(cred.client_id, safe='')}:{quote(cred.client_secret, safe='')}"
        return form, {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}


def _json(response: httpx.Response) -> Mapping[str, object]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _token_of(body: Mapping[str, object], server: str) -> tuple[str, float]:
    token, kind, lifetime = body.get("access_token"), body.get("token_type"), body.get("expires_in")
    if not isinstance(token, str) or not token:
        raise ServiceTokenUnavailable(f"{server}: token response has no access_token")
    if not isinstance(kind, str) or kind.lower() != "bearer":
        raise ServiceTokenUnavailable(f"{server}: token response is not a bearer token")
    seconds = (
        float(lifetime)
        if isinstance(lifetime, int | float) and not isinstance(lifetime, bool)
        else DEFAULT_LIFETIME_SECONDS
    )
    return token, seconds


class ServiceBearerAuth(httpx2.Auth):
    """Sets the bearer on each request; on a 401, re-mints once and retries."""

    def __init__(self, source: ServiceTokenSource) -> None:
        self._source = source

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        token = await self._source.bearer()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code != 401:
            return
        log.info("federation.service_token.rejected", server=self._source.server)
        self._source.invalidate(token)
        request.headers["Authorization"] = f"Bearer {await self._source.bearer()}"
        yield request


def authenticated_session_factory(
    sources: Mapping[str, ServiceTokenSource],
    fallback: SessionFactory | None = None,
    *,
    http_transport: httpx2.AsyncBaseTransport | None = None,
) -> SessionFactory:
    """Open credentialled servers with their own bearer; others via `fallback`.

    `http_transport` is for tests, which put an in-process server behind it.
    """
    remote = fallback or default_session_factory

    @asynccontextmanager
    async def _open(server: ServerConfig) -> AsyncIterator[ToolSession]:
        source = sources.get(server.name)
        if source is None:
            async with remote(server) as fallen_back:
                yield fallen_back
            return
        async with httpx2.AsyncClient(
            auth=ServiceBearerAuth(source),
            transport=http_transport,
            timeout=httpx2.Timeout(30.0, read=300.0),
        ) as http:
            client = Client(
                streamable_http_client(server.target, http_client=http),
                read_timeout_seconds=server.timeout_seconds,
            )
            async with open_session(client) as session:
                yield session

    return _open
