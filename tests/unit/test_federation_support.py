"""Fakes and a stack builder shared by the federation, authorization and
injection suites.

Holds no tests of its own. The fake server is deliberately *capable of
misbehaving* -- hanging, dying between calls, returning oversized text,
lying in its annotations -- because a fake that only does the right thing
proves nothing about code whose whole job is surviving the wrong thing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from chatmemory.adapters.mcp_client.client import Federation, SessionFactory, connect
from chatmemory.adapters.mcp_client.config import (
    AllowedTool,
    FederationConfig,
    ServerConfig,
)
from chatmemory.adapters.mcp_client.invoker import GuardedInvoker
from chatmemory.adapters.mcp_client.routing import RoutedTools, ToolRouter
from chatmemory.adapters.mcp_client.session import DiscoveredTool, ToolResult, ToolSession
from chatmemory.app.audit import InMemoryAuditTrail
from chatmemory.app.authorization import (
    Authorizer,
    ConfirmationLedger,
    CredentialBroker,
    CredentialScope,
    ToolEffect,
)
from chatmemory.domain.identity import PersonRef

ALICE = PersonRef("discord", 1001)
MALLORY = PersonRef("discord", 1666)


def read_tool(name: str, description: str = "") -> DiscoveredTool:
    return DiscoveredTool(name=name, description=description, effect=ToolEffect.READ_ONLY)


def write_tool(name: str, description: str = "") -> DiscoveredTool:
    return DiscoveredTool(name=name, description=description, effect=ToolEffect.MUTATING)


def silent_tool(name: str, description: str = "") -> DiscoveredTool:
    """A tool whose server never says whether it writes. Counts as mutating."""
    return DiscoveredTool(name=name, description=description, effect=ToolEffect.UNDETERMINED)


@dataclass
class FakeSession:
    """A connected server that can be made to fail in each way that matters."""

    tools: tuple[DiscoveredTool, ...] = ()
    responses: dict[str, str] = field(default_factory=dict)
    errors: set[str] = field(default_factory=set)
    hang_seconds: float = 0.0
    die_after: int | None = None
    calls: list[tuple[str, Mapping[str, object]]] = field(default_factory=list)

    async def list_tools(self) -> Sequence[DiscoveredTool]:
        return self.tools

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if self.die_after is not None and len(self.calls) > self.die_after:
            raise ConnectionResetError("the server went away")
        if self.hang_seconds:
            await asyncio.sleep(self.hang_seconds)
        if name in self.errors:
            return ToolResult(text="boom", is_error=True)
        return ToolResult(text=self.responses.get(name, f"{name} says hello"))

    @property
    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]


def session_factory(sessions: Mapping[str, ToolSession]) -> SessionFactory:
    """Open the named fake, or fail the way an unreachable server fails."""

    @asynccontextmanager
    async def _open(config: ServerConfig) -> AsyncIterator[ToolSession]:
        session = sessions.get(config.name)
        if session is None:
            raise ConnectionRefusedError(f"{config.name} is not listening")
        yield session

    return _open


@dataclass(frozen=True, slots=True)
class Stack:
    """Everything a run needs, wired the way production wires it."""

    config: FederationConfig
    federation: Federation
    invoker: GuardedInvoker
    audit: InMemoryAuditTrail
    confirmations: ConfirmationLedger
    credentials: CredentialBroker
    authorizer: Authorizer

    def route(self, question: str) -> RoutedTools:
        return ToolRouter(self.config.max_tools_per_run).route(
            question, self.federation.registration
        )

    def offer_all(self) -> RoutedTools:
        """Offer every registered tool, bypassing relevance.

        Used where the test is about the invoke-time gate rather than about
        routing: an injection that only fails because the router happened not
        to surface the tool would be a coincidence, not a defence.
        """
        return RoutedTools(question="", tools=self.federation.registration.tools)


async def build_stack(
    servers: Sequence[ServerConfig],
    allowlist: Sequence[AllowedTool],
    sessions: Mapping[str, ToolSession],
    *,
    credential_holders: Mapping[str, frozenset[PersonRef]] | None = None,
    max_tools_per_run: int = 5,
    max_result_chars: int = 4000,
) -> Stack:
    config = FederationConfig(
        servers=tuple(servers),
        allowlist=tuple(allowlist),
        max_tools_per_run=max_tools_per_run,
        max_result_chars=max_result_chars,
    )
    federation = await connect(config, session_factory(sessions))
    confirmations = ConfirmationLedger()
    credentials = CredentialBroker(credential_holders or {})
    authorizer = Authorizer(federation.permits, confirmations, credentials)
    audit = InMemoryAuditTrail()
    return Stack(
        config=config,
        federation=federation,
        invoker=GuardedInvoker(federation, authorizer, audit, confirmations),
        audit=audit,
        confirmations=confirmations,
        credentials=credentials,
        authorizer=authorizer,
    )


def per_requester(server: str, tool: str, *, mutating: bool = False) -> AllowedTool:
    return AllowedTool(
        server=server,
        tool=tool,
        credential=CredentialScope.PER_REQUESTER,
        mutation_enabled=mutating,
    )


def read_only(server: str, tool: str) -> AllowedTool:
    return AllowedTool(server=server, tool=tool, credential=CredentialScope.NARROW_READ_ONLY)
