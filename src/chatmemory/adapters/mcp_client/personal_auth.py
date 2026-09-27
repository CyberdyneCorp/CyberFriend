"""One person's own key as the bearer, on a connection opened for their call alone.

The shared connection to a server carries the deployment's service identity
and is used by every asker. A personal (`my_*`) call must carry the asker's
own connected-app key instead, and that key must not ride a connection other
people's calls use. So each keyed call opens its own streamable-HTTP client,
sets the key on it, makes the one call and closes it. The key never reaches a
default header, a shared client, the token endpoint or a log line.

A `401` is remembered by the auth object, because the MCP SDK turns it into a
generic error: `PersonalKeyRejected` is how the caller tells "your key was
refused" (expired, revoked) from "the server is down".
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from chatmemory.adapters.mcp_client.config import ServerConfig
from chatmemory.adapters.mcp_client.session import ToolSession, open_session

KeyedSessionFactory = Callable[[ServerConfig, str], AbstractAsyncContextManager[ToolSession]]
"""Open `server` for one call with `key` as the bearer."""


class PersonalKeyRejected(Exception):
    """The server answered 401 to the person's own key."""


class PersonalBearerAuth(httpx2.Auth):
    """The person's key on every request of this one client; a 401 is noted, not retried."""

    def __init__(self, key: str) -> None:
        self._key = key
        self.rejected = False

    def __repr__(self) -> str:
        return "PersonalBearerAuth(<hidden>)"

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {self._key}"
        response = yield request
        if response.status_code == 401:
            self.rejected = True


def keyed_session_factory(
    *, http_transport: httpx2.AsyncBaseTransport | None = None
) -> KeyedSessionFactory:
    """Open a server over streamable HTTP with a person's key, for one call.

    `http_transport` is for tests, which put an in-process server behind it.
    """

    @asynccontextmanager
    async def _open(server: ServerConfig, key: str) -> AsyncIterator[ToolSession]:
        auth = PersonalBearerAuth(key)
        try:
            async with httpx2.AsyncClient(
                auth=auth,
                transport=http_transport,
                timeout=httpx2.Timeout(30.0, read=300.0),
            ) as http:
                client = Client(
                    streamable_http_client(server.target, http_client=http),
                    read_timeout_seconds=server.timeout_seconds,
                )
                async with open_session(client) as session:
                    yield session
        except Exception as exc:
            if auth.rejected:
                # `from None`: the SDK's error chain is noise here, and a
                # rejected key is fully described by its server's name.
                raise PersonalKeyRejected(server.name) from None
            raise exc

    return _open
