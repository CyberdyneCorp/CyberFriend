"""What CyberFriend is allowed to reach, and with whose authority.

Configuration is validated when it loads, not when a request fails. Every
rule below is a startup error rather than a runtime surprise, because the
alternative is an operator discovering at 3am that a tool they thought was
enabled was quietly dropped six deploys ago.

The vocabulary here is deliberately closed: a server, a tool, a credential
scope, and a boolean for mutation. There is no expression language and no
word for "allow anything from this server", so "edit the config to permit X"
is a sentence that can only be written one tool at a time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from chatmemory.app.authorization import CredentialScope, ToolEffect

QUALIFIER = ":"
"""Separator between server and tool name.

Excluded from both name patterns below, so a qualified name parses back into
exactly one (server, tool) pair and a server called `a:b` cannot be forged.
"""

_SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ConfigurationError(Exception):
    """Configuration that cannot be honoured. Raised at load, never at request time."""


def qualify(server: str, tool: str) -> str:
    return f"{server}{QUALIFIER}{tool}"


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """One external MCP server CyberFriend may connect to.

    `target` is whatever the session factory knows how to open -- a URL for
    an HTTP transport, a command for stdio. The federation layer never parses
    it, so adding a transport does not change anything above this line.
    """

    name: str
    target: str
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not _SERVER_NAME.match(self.name):
            raise ConfigurationError(
                f"server name {self.name!r} must be lowercase alphanumeric, "
                f"'_' or '-', and must not contain {QUALIFIER!r}"
            )
        if not self.target:
            raise ConfigurationError(f"server {self.name!r} has no target")
        if self.timeout_seconds <= 0:
            raise ConfigurationError(f"server {self.name!r} needs a positive timeout")


@dataclass(frozen=True, slots=True)
class AllowedTool:
    """One tool an operator has explicitly made available.

    `effect` is the operator's own declaration. It is combined with whatever
    the server claims by taking the *stricter* of the two, so a server can
    tighten its own tools but never loosen the operator's judgement -- the
    same union-never-substitute rule the corrective policy follows.
    """

    server: str
    tool: str
    credential: CredentialScope = CredentialScope.NARROW_READ_ONLY
    effect: ToolEffect | None = None
    mutation_enabled: bool = False

    def __post_init__(self) -> None:
        if not _SERVER_NAME.match(self.server):
            raise ConfigurationError(f"allowlist entry names invalid server {self.server!r}")
        if not _TOOL_NAME.match(self.tool):
            raise ConfigurationError(
                f"tool name {self.tool!r} must not be empty or contain {QUALIFIER!r}"
            )
        if self.credential is CredentialScope.AMBIENT:
            # Ambient org-wide credentials on the bot mean every server member
            # wields them through it. Refused here as well as at invocation:
            # the runtime check catches a permit built in code, this one
            # catches the deployment that would have made it routine.
            raise ConfigurationError(
                f"{qualify(self.server, self.tool)}: ambient credentials are never "
                "granted; use per-requester or a narrow read-only identity"
            )
        if self.mutation_enabled and self.credential is CredentialScope.NARROW_READ_ONLY:
            raise ConfigurationError(
                f"{qualify(self.server, self.tool)}: a read-only identity cannot be "
                "granted mutation; mutating tools must act per-requester"
            )
        if self.effect is ToolEffect.READ_ONLY and self.mutation_enabled:
            raise ConfigurationError(
                f"{qualify(self.server, self.tool)}: declared read-only but "
                "mutation_enabled is set"
            )

    @property
    def qualified_name(self) -> str:
        return qualify(self.server, self.tool)


@dataclass(frozen=True, slots=True)
class FederationConfig:
    """The complete outbound surface.

    An empty config is valid and means "no federation": CyberFriend answers
    from its own corpus, which is the correct default for a deployment that
    has not thought about external credentials yet.
    """

    servers: tuple[ServerConfig, ...] = ()
    allowlist: tuple[AllowedTool, ...] = ()
    max_tools_per_run: int = 5
    max_result_chars: int = 4000
    call_timeout_seconds: float = 15.0
    _by_name: dict[str, ServerConfig] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_tools_per_run < 1:
            raise ConfigurationError("max_tools_per_run must be at least 1")
        if self.max_result_chars < 1:
            raise ConfigurationError("max_result_chars must be at least 1")
        if self.call_timeout_seconds <= 0:
            raise ConfigurationError("call_timeout_seconds must be positive")

        by_name: dict[str, ServerConfig] = {}
        for server in self.servers:
            if server.name in by_name:
                raise ConfigurationError(f"duplicate server {server.name!r}")
            by_name[server.name] = server
        self._by_name.update(by_name)

        seen: set[str] = set()
        for entry in self.allowlist:
            if entry.server not in by_name:
                raise ConfigurationError(
                    f"{entry.qualified_name} names server {entry.server!r}, "
                    "which is not configured"
                )
            if entry.qualified_name in seen:
                raise ConfigurationError(f"duplicate allowlist entry {entry.qualified_name}")
            seen.add(entry.qualified_name)

    def server(self, name: str) -> ServerConfig | None:
        return self._by_name.get(name)

    def allowed_for(self, server: str) -> tuple[AllowedTool, ...]:
        return tuple(e for e in self.allowlist if e.server == server)

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.servers)

    def timeout_for(self, server: str) -> float:
        found = self._by_name.get(server)
        return found.timeout_seconds if found else self.call_timeout_seconds


def build_config(
    servers: Iterable[ServerConfig],
    allowlist: Iterable[AllowedTool],
    *,
    max_tools_per_run: int = 5,
    max_result_chars: int = 4000,
    call_timeout_seconds: float = 15.0,
) -> FederationConfig:
    """Assemble and validate a federation config from iterables."""
    return FederationConfig(
        servers=tuple(servers),
        allowlist=tuple(allowlist),
        max_tools_per_run=max_tools_per_run,
        max_result_chars=max_result_chars,
        call_timeout_seconds=call_timeout_seconds,
    )


def split_qualified(qualified_name: str) -> tuple[str, str]:
    """Split `server:tool`. Returns empty halves for anything malformed."""
    server, sep, tool = qualified_name.partition(QUALIFIER)
    if not sep:
        return ("", "")
    return (server, tool)


def qualified_names(entries: Sequence[AllowedTool]) -> tuple[str, ...]:
    return tuple(e.qualified_name for e in entries)
