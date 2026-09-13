"""CyberFriend as an MCP *client*: reaching out to other teams' servers.

Not to be confused with `chatmemory.mcp`, which is the inbound surface --
CyberFriend as a server other clients call. The two are named apart on
purpose, because conflating "tools we offer" with "tools we use" is how an
external server's tool ends up trusted like our own.

Read in dependency order:

    config     what an operator allowed, validated at load
    session    the thin edge against one real server
    registry   allowlist x discovery -> the callable surface
    routing    which of those a single run may see
    client     connecting, calling, timing out, degrading
    invoker    the guarded door: authorize, invoke, audit
"""

from __future__ import annotations

from chatmemory.adapters.mcp_client.client import (
    Failure,
    FederatedResult,
    Federation,
    connect,
)
from chatmemory.adapters.mcp_client.config import (
    AllowedTool,
    ConfigurationError,
    FederationConfig,
    ServerConfig,
    build_config,
    qualify,
)
from chatmemory.adapters.mcp_client.invoker import GuardedInvoker, InvocationOutcome
from chatmemory.adapters.mcp_client.registry import (
    FederationStartupError,
    RegisteredTool,
    Registration,
    ServerDiscovery,
    register,
)
from chatmemory.adapters.mcp_client.routing import RoutedTools, ToolRouter
from chatmemory.adapters.mcp_client.session import (
    DiscoveredTool,
    MCPToolSession,
    ToolResult,
    ToolSession,
    open_session,
)

__all__ = [
    "AllowedTool",
    "ConfigurationError",
    "DiscoveredTool",
    "Failure",
    "FederatedResult",
    "Federation",
    "FederationConfig",
    "FederationStartupError",
    "GuardedInvoker",
    "InvocationOutcome",
    "MCPToolSession",
    "RegisteredTool",
    "Registration",
    "RoutedTools",
    "ServerConfig",
    "ServerDiscovery",
    "ToolResult",
    "ToolRouter",
    "ToolSession",
    "build_config",
    "connect",
    "open_session",
    "qualify",
    "register",
]
