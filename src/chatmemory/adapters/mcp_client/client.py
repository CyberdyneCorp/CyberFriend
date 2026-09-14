"""Connecting out to other MCP servers, and surviving them.

Every failure mode an external dependency has, this module has to hold: a
server that is down when we boot, one that hangs mid-call, one that returns a
megabyte, and one that dies between two calls in the same run. The rule
running through all of them is that an external system's *absence* must never
be rendered as an absence of *information* -- "GitHub did not answer" and
"nobody mentioned that" are different answers, and conflating them is how a
bot becomes confidently wrong.

Connections are established one at a time and independently. A single
`asyncio.gather` over all servers would let one hanging handshake delay every
healthy server's tools, which is the failure that hides capability rather
than reporting it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

import structlog

from chatmemory.adapters.mcp_client.config import FederationConfig, ServerConfig
from chatmemory.adapters.mcp_client.registry import (
    Registration,
    ServerDiscovery,
    register,
)
from chatmemory.adapters.mcp_client.session import ToolSession, default_session_factory
from chatmemory.app.authorization import ContentKind, FencedContent, ToolPermit, fence

log = structlog.get_logger()

SessionFactory = Callable[[ServerConfig], AbstractAsyncContextManager[ToolSession]]

TRUNCATION_SUFFIX = "\n[... truncated]"


class Failure(StrEnum):
    """Why a federated call produced nothing. Never rendered as an empty result."""

    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    TOOL_ERROR = "tool_error"


@dataclass(frozen=True, slots=True)
class FederatedResult:
    """One external call's outcome, attributed to the system that produced it."""

    qualified_name: str
    server: str
    tool: str
    ok: bool
    text: str = ""
    truncated: bool = False
    failure: Failure | None = None

    @property
    def attribution(self) -> str:
        """How the answer names this contribution."""
        return f"{self.server} ({self.tool})"

    def as_evidence(self) -> FencedContent:
        """Fence the result so it enters context as data.

        Every path out of this module that a prompt can reach goes through
        here. There is no accessor returning the raw text for insertion into
        an instruction region.
        """
        return fence(
            ContentKind.TOOL_RESULT,
            origin=self.qualified_name,
            body=self.text,
            truncated=self.truncated,
        )

    def notice(self) -> str | None:
        """What the answer must say about this call, if anything."""
        if self.failure is Failure.TIMEOUT:
            return f"{self.server} did not respond in time; its data is missing from this answer."
        if self.failure is Failure.UNAVAILABLE:
            return f"{self.server} was unavailable; its data is missing from this answer."
        if self.failure is Failure.TOOL_ERROR:
            return f"{self.server} reported an error for {self.tool}."
        if self.truncated:
            return f"The result from {self.server} was truncated to fit."
        return None


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Clip an external result, reporting whether anything was dropped."""
    if len(text) <= limit:
        return text, False
    return text[:limit] + TRUNCATION_SUFFIX, True


class Federation:
    """The live outbound surface: connected sessions plus the registered tools.

    Owns no credentials of its own beyond what the session factory opens, and
    holds no route to the corpus. The reasoning loop reaches Discord content
    through the inbound MCP interface like any other client; this object is
    strictly about everything that is not Discord.
    """

    def __init__(
        self,
        config: FederationConfig,
        sessions: Mapping[str, ToolSession],
        registration: Registration,
        stack: AsyncExitStack | None = None,
    ) -> None:
        self._config = config
        self._sessions = dict(sessions)
        self._registration = registration
        self._stack = stack
        # Servers that failed *after* connecting. Kept separate from the
        # startup set so a mid-run death is visible in the answer as a
        # different fact from "it was never up".
        self._lost: dict[str, Failure] = {}

    @property
    def registration(self) -> Registration:
        return self._registration

    @property
    def permits(self) -> Mapping[str, ToolPermit]:
        return self._registration.permits

    @property
    def unreachable_servers(self) -> tuple[str, ...]:
        return self._registration.unreachable_servers

    @property
    def lost_servers(self) -> Mapping[str, Failure]:
        return dict(self._lost)

    def degradation_notices(self) -> tuple[str, ...]:
        """What an answer must disclose about systems it could not reach."""
        startup = tuple(
            f"{name} was unavailable; its data is missing from this answer."
            for name in self._registration.unreachable_servers
        )
        midrun = tuple(
            f"{name} stopped responding during this run; its data may be incomplete."
            for name in sorted(self._lost)
        )
        return startup + midrun

    async def call(
        self, permit: ToolPermit, arguments: Mapping[str, object]
    ) -> FederatedResult:
        """Invoke one registered tool, bounded in time and in size.

        Authorization is *not* checked here. This is the transport; the gate
        is `GuardedInvoker`, which is the only caller in production. Keeping
        them apart means the gate cannot be accidentally satisfied by the
        thing it is supposed to guard.
        """
        session = self._sessions.get(permit.server)
        if session is None or permit.server in self._lost:
            return self._failed(permit, Failure.UNAVAILABLE)

        timeout = self._config.timeout_for(permit.server)
        try:
            # `asyncio.timeout` rather than `wait_for`: wait_for runs the call
            # in a *new* task, and the MCP SDK's anyio cancel scopes must be
            # entered and exited in the same task or teardown raises instead of
            # closing. A timeout must not corrupt the connection it bounds.
            async with asyncio.timeout(timeout):
                result = await session.call_tool(permit.tool, arguments)
        except TimeoutError:
            # Not marked lost: a slow call is not a dead server, and one
            # expensive query should not remove the rest of its tools.
            log.warning("federation.call.timeout", tool=permit.qualified_name, timeout=timeout)
            return self._failed(permit, Failure.TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - any transport error degrades the same way
            log.warning(
                "federation.call.failed", tool=permit.qualified_name, error=str(exc)
            )
            self._lost[permit.server] = Failure.UNAVAILABLE
            return self._failed(permit, Failure.UNAVAILABLE)

        if result.is_error:
            return self._failed(permit, Failure.TOOL_ERROR)

        text, truncated = truncate(result.text, self._config.max_result_chars)
        return FederatedResult(
            qualified_name=permit.qualified_name,
            server=permit.server,
            tool=permit.tool,
            ok=True,
            text=text,
            truncated=truncated,
        )

    def _failed(self, permit: ToolPermit, failure: Failure) -> FederatedResult:
        return FederatedResult(
            qualified_name=permit.qualified_name,
            server=permit.server,
            tool=permit.tool,
            ok=False,
            failure=failure,
        )

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    async def __aenter__(self) -> Federation:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


async def connect(
    config: FederationConfig,
    factory: SessionFactory | None = None,
) -> Federation:
    """Open every configured server, then register the allowlist against it.

    Each server is entered in its own `try`, so one refusing connections, one
    timing out on its handshake, and one returning nonsense all leave the
    others working. The startup error that *can* stop a boot is a different
    thing entirely: a reachable server that does not provide a tool the
    operator listed (see `registry.register`).
    """
    open_session = factory or default_session_factory
    stack = AsyncExitStack()
    sessions: dict[str, ToolSession] = {}
    discovery: list[ServerDiscovery] = []

    for server in config.servers:
        try:
            async with asyncio.timeout(server.timeout_seconds):
                session = await stack.enter_async_context(open_session(server))
                tools = tuple(await session.list_tools())
        except Exception as exc:  # noqa: BLE001 - every startup failure degrades alike
            log.warning("federation.connect.failed", server=server.name, error=str(exc))
            discovery.append(ServerDiscovery(name=server.name, failure=str(exc) or "unavailable"))
            continue
        sessions[server.name] = session
        discovery.append(ServerDiscovery(name=server.name, tools=tools))
        log.info("federation.connected", server=server.name, tools=len(tools))

    try:
        registration = register(config, discovery)
    except Exception:
        # A startup error must not leave half-open transports behind.
        await stack.aclose()
        raise

    return Federation(config, sessions, registration, stack)
