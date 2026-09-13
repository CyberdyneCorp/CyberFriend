"""Turning "this server has tools" into "the loop may call this tool".

The distinction this module exists to enforce: **discovery is not
registration**. A connected server advertising fifty tools grants the agent
nothing; only the tools an operator wrote down become callable, under names
qualified by their server so two servers' `search` can never be confused.

The converse case is the one that needs care. A tool the operator listed but
nobody provides has two very different explanations:

*   The server answered and does not have it -- someone renamed or removed a
    tool, and the deployment is now quietly less capable than its
    configuration claims. That is a **startup error**.
*   The server never answered -- it is down. That is **degradation**: the
    tool is unavailable and recorded as such, and the rest of the deployment
    still runs, because refusing to boot when one of five servers is
    restarting would make federation a single point of failure for answering
    questions about Discord.

Collapsing those two into one behaviour gets one of them wrong.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import structlog

from chatmemory.adapters.mcp_client.config import AllowedTool, FederationConfig
from chatmemory.adapters.mcp_client.session import DiscoveredTool
from chatmemory.app.authorization import ToolPermit

log = structlog.get_logger()


class FederationStartupError(Exception):
    """Configuration and reality disagree in a way that must not be ignored."""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """An allowlisted tool that a reachable server actually provides."""

    permit: ToolPermit
    description: str

    @property
    def qualified_name(self) -> str:
        return self.permit.qualified_name

    @property
    def server(self) -> str:
        return self.permit.server


@dataclass(frozen=True, slots=True)
class ServerDiscovery:
    """What one server told us, or why it told us nothing."""

    name: str
    tools: tuple[DiscoveredTool, ...] = ()
    failure: str | None = None

    @property
    def reachable(self) -> bool:
        return self.failure is None


@dataclass(frozen=True, slots=True)
class Registration:
    """The callable surface, plus an honest account of what is missing."""

    tools: tuple[RegisteredTool, ...]
    unreachable_servers: tuple[str, ...] = ()
    unavailable_tools: tuple[str, ...] = ()

    @property
    def permits(self) -> Mapping[str, ToolPermit]:
        return {t.qualified_name: t.permit for t in self.tools}

    @property
    def names(self) -> frozenset[str]:
        return frozenset(t.qualified_name for t in self.tools)

    def get(self, qualified_name: str) -> RegisteredTool | None:
        return next((t for t in self.tools if t.qualified_name == qualified_name), None)

    @property
    def degraded(self) -> bool:
        return bool(self.unreachable_servers)


def register(config: FederationConfig, discovery: Sequence[ServerDiscovery]) -> Registration:
    """Match the allowlist against what each server reported.

    Raises `FederationStartupError` when a reachable server does not provide
    a tool the configuration lists. Tools behind an unreachable server are
    returned as `unavailable_tools` instead, so the caller can say which
    capability is missing and why.
    """
    reported = {d.name: d for d in discovery}
    registered: list[RegisteredTool] = []
    unavailable: list[str] = []
    missing: list[str] = []

    for entry in config.allowlist:
        found = reported.get(entry.server)
        if found is None or not found.reachable:
            reason = "not connected" if found is None else (found.failure or "unavailable")
            log.warning(
                "federation.tool_unavailable",
                tool=entry.qualified_name,
                server=entry.server,
                reason=reason,
            )
            unavailable.append(entry.qualified_name)
            continue

        advertised = next((t for t in found.tools if t.name == entry.tool), None)
        if advertised is None:
            missing.append(entry.qualified_name)
            continue

        registered.append(_register_one(entry, advertised))

    if missing:
        raise FederationStartupError(
            "configured tools are not provided by their servers: " + ", ".join(sorted(missing))
        )

    for found in discovery:
        listed = {e.tool for e in config.allowed_for(found.name)}
        ignored = sorted(t.name for t in found.tools if t.name not in listed)
        if ignored:
            # Logged, never registered: this is the line a server crosses when
            # it adds a tool after deployment, and crossing it must require a
            # configuration change rather than a restart.
            log.info("federation.tools_not_allowlisted", server=found.name, tools=ignored)

    unreachable = tuple(d.name for d in discovery if not d.reachable)
    return Registration(
        tools=tuple(registered),
        unreachable_servers=unreachable,
        unavailable_tools=tuple(unavailable),
    )


def _register_one(entry: AllowedTool, advertised: DiscoveredTool) -> RegisteredTool:
    # With no operator declaration the server's own annotation decides, and a
    # server that annotates nothing lands on UNDETERMINED -- which behaves as
    # mutating. With a declaration, the stricter of the two wins.
    effect = (
        advertised.effect
        if entry.effect is None
        else entry.effect.stricter(advertised.effect)
    )
    return RegisteredTool(
        permit=ToolPermit(
            qualified_name=entry.qualified_name,
            server=entry.server,
            tool=entry.tool,
            effect=effect,
            credential=entry.credential,
            mutation_enabled=entry.mutation_enabled,
        ),
        # The description is used only for routing. It is text an external
        # server controls, so it never reaches a decision about permissions.
        description=advertised.description,
    )
