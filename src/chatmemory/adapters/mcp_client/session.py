"""The thin edge against a real MCP server.

Everything above this module speaks `ToolSession`, which knows about two
operations and no transport. That keeps the federation logic testable
against fakes *and* against a genuine in-process MCP server, which is how the
tests exercise the wire format without a network.

One judgement lives here: a server's tool annotations are *claims by the
server*, not facts. They are read to decide whether a tool needs
confirmation, and a missing or ambiguous claim resolves to `UNDETERMINED`,
which behaves as mutating everywhere a decision is made.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

import structlog
from mcp.client import Client
from mcp.types import TextContent, ToolAnnotations

from chatmemory.adapters.mcp_client.config import ServerConfig
from chatmemory.app.authorization import ToolEffect

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class DiscoveredTool:
    """A tool a server says it has. Discovery alone confers nothing."""

    name: str
    description: str
    effect: ToolEffect


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The text a tool returned, plus whether the server flagged it an error."""

    text: str
    is_error: bool = False


class ToolSession(Protocol):
    """One connected server, reduced to what federation needs of it."""

    async def list_tools(self) -> Sequence[DiscoveredTool]:
        """Tools this server advertises right now."""
        ...

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        """Invoke one tool by its *unqualified* name on this server."""
        ...


def effect_of(annotations: ToolAnnotations | None) -> ToolEffect:
    """Read a server's read-only hint, failing closed when it is silent."""
    if annotations is None or annotations.read_only_hint is None:
        # No claim is not a claim of safety. An unannotated tool is treated
        # as mutating, so a server that simply never sets the hint costs a
        # confirmation prompt rather than an unreviewed write.
        return ToolEffect.UNDETERMINED
    return ToolEffect.READ_ONLY if annotations.read_only_hint else ToolEffect.MUTATING


class MCPToolSession:
    """`ToolSession` over the MCP SDK client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def list_tools(self) -> Sequence[DiscoveredTool]:
        listing = await self._client.list_tools()
        return [
            DiscoveredTool(
                name=tool.name,
                description=tool.description or "",
                effect=effect_of(tool.annotations),
            )
            for tool in listing.tools
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        result = await self._client.call_tool(name, dict(arguments))
        # Only text blocks are read. An image or embedded resource from an
        # external server is not something the reasoning loop can cite, and
        # silently rendering it into the prompt would smuggle unfenced bytes
        # into context.
        chunks = [c.text for c in result.content if isinstance(c, TextContent)]
        return ToolResult(text="\n".join(chunks), is_error=bool(result.is_error))


@asynccontextmanager
async def open_session(client: Client) -> AsyncIterator[ToolSession]:
    """Enter a client and expose it as a `ToolSession` for the duration."""
    async with client:
        yield MCPToolSession(client)


def default_session_factory(config: ServerConfig) -> AbstractAsyncContextManager[ToolSession]:
    """Open a connection to a configured server.

    The only place a `target` string is interpreted. Tests substitute a
    factory that hands back an in-process server, which is why nothing above
    here needs a network to be exercised.
    """
    return open_session(Client(config.target, read_timeout_seconds=config.timeout_seconds))
