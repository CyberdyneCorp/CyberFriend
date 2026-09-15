"""Web tools, assembled as federation configuration.

The output of this module is deliberately unremarkable: a tuple of
`ServerConfig`, a tuple of `AllowedTool`, and a `SessionFactory`. Those are
the three things `connect()` already takes, so a web tool is registered,
routed, authorized and audited by exactly the code that governs a remote MCP
server. Nothing downstream needs to know that these two "servers" run in
process.

Two decisions are made here and nowhere else:

*   **Read-only is declared by us, not claimed by them.** `effect` is set on
    the allowlist entry, which is the only place the registry accepts a
    read-only determination from. A provider that later started mutating
    something could not relabel itself.
*   **SerpApi exists only if its key does.** No key, no `ServerConfig`, no
    allowlist entry, nothing in the registry. The alternative -- registering
    it and failing every call -- teaches the reasoning loop to plan around a
    capability the deployment does not have.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace

import httpx
import structlog

from chatmemory.adapters.mcp_client.client import SessionFactory
from chatmemory.adapters.mcp_client.config import (
    AllowedTool,
    FederationConfig,
    ServerConfig,
)
from chatmemory.adapters.mcp_client.session import ToolSession, default_session_factory
from chatmemory.adapters.web.limits import (
    DEFAULT_CALLS_PER_RUN,
    DEFAULT_MIN_INTERVAL,
    CallBudget,
    RateLimiter,
)
from chatmemory.adapters.web.provider import (
    DEFAULT_MAX_RESULT_CHARS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_TIMEOUT,
    WebProvider,
)
from chatmemory.adapters.web.results import SERPAPI_SERVER, WIKIPEDIA_SERVER
from chatmemory.adapters.web.serpapi import (
    SERPAPI_ENDPOINT,
    SerpApiProvider,
)
from chatmemory.adapters.web.serpapi import (
    TOOLS as SERPAPI_TOOLS,
)
from chatmemory.adapters.web.wikipedia import (
    TOOLS as WIKIPEDIA_TOOLS,
)
from chatmemory.adapters.web.wikipedia import (
    WIKIPEDIA_ENDPOINT,
    WikipediaProvider,
)
from chatmemory.app.authorization import CredentialScope, ToolEffect

log = structlog.get_logger()

TIMEOUT_HEADROOM = 2.0
"""How much longer the federation client waits than the provider does.

Both bound the same call. Giving the provider the shorter budget means a slow
answer comes back as this module's own degradation -- logged, attributed, and
with the connection intact -- rather than being cut from outside.
"""


@dataclass(frozen=True, slots=True)
class WebToolsConfig:
    """Everything an operator can set about the egress boundary."""

    serpapi_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT
    max_results: int = DEFAULT_MAX_RESULTS
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS
    max_calls_per_run: int = DEFAULT_CALLS_PER_RUN
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL
    wikipedia_endpoint: str = WIKIPEDIA_ENDPOINT
    serpapi_endpoint: str = SERPAPI_ENDPOINT


@dataclass(frozen=True, slots=True)
class WebTools:
    """The web providers, ready to be handed to `connect()`."""

    servers: tuple[ServerConfig, ...] = ()
    allowlist: tuple[AllowedTool, ...] = ()
    providers: Mapping[str, WebProvider] = field(default_factory=dict)

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.servers)

    def factory(self, fallback: SessionFactory | None = None) -> SessionFactory:
        """A session factory that opens these providers locally.

        Anything it does not recognise goes to `fallback`, so one factory
        serves a deployment that federates to real MCP servers as well.
        """
        remote = fallback or default_session_factory
        providers = self.providers

        @asynccontextmanager
        async def _open(server: ServerConfig) -> AsyncIterator[ToolSession]:
            provider = providers.get(server.name)
            if provider is None:
                async with remote(server) as session:
                    yield session
                return
            async with provider.opened() as session:
                yield session

        return _open

    def merge_into(self, config: FederationConfig) -> FederationConfig:
        """Add these servers and tools to an existing federation config.

        Returns a new config, validated by `FederationConfig` itself: a name
        that collided with a configured MCP server is a startup error, not a
        silent override.
        """
        return replace(
            config,
            servers=(*config.servers, *self.servers),
            allowlist=(*config.allowlist, *self.allowlist),
            # The private index is rebuilt by __post_init__; carrying the old
            # one over would leave it holding the pre-merge servers.
            _by_name={},
        )


def _entry(server: str, tool: str) -> AllowedTool:
    return AllowedTool(
        server=server,
        tool=tool,
        # The deployment's own narrow identity: Wikipedia has none at all and
        # SerpApi's key buys the same public results for everyone, so there is
        # nothing here a requester could be lent.
        credential=CredentialScope.NARROW_READ_ONLY,
        # The operator's determination, which is what the registry honours.
        effect=ToolEffect.READ_ONLY,
        mutation_enabled=False,
    )


def build_web_tools(
    config: WebToolsConfig | None = None, *, client: httpx.AsyncClient | None = None
) -> WebTools:
    """Assemble whichever web providers this deployment can actually run.

    `client` lets a caller own the HTTP client -- a shared connection pool in
    production, a transport that never reaches the network in tests. Left
    unset, each provider opens and closes its own for as long as it is
    registered.
    """
    settings = config or WebToolsConfig()
    # One budget across providers: what is being capped is egress per
    # question, not any single vendor's quota.
    budget = CallBudget(settings.max_calls_per_run)
    servers: list[ServerConfig] = []
    allowlist: list[AllowedTool] = []
    providers: dict[str, WebProvider] = {}

    wikipedia = WikipediaProvider(
        budget,
        endpoint=settings.wikipedia_endpoint,
        limiter=RateLimiter(settings.min_interval_seconds),
        max_results=settings.max_results,
        max_result_chars=settings.max_result_chars,
        timeout_seconds=settings.timeout_seconds,
        client=client,
    )
    providers[WIKIPEDIA_SERVER] = wikipedia
    servers.append(_server(WIKIPEDIA_SERVER, settings.wikipedia_endpoint, settings))
    allowlist.extend(_entry(WIKIPEDIA_SERVER, tool.name) for tool in WIKIPEDIA_TOOLS)

    key = (settings.serpapi_key or "").strip()
    if key:
        serpapi = SerpApiProvider(
            key,
            budget,
            endpoint=settings.serpapi_endpoint,
            limiter=RateLimiter(settings.min_interval_seconds),
            max_results=settings.max_results,
            max_result_chars=settings.max_result_chars,
            timeout_seconds=settings.timeout_seconds,
            client=client,
        )
        providers[SERPAPI_SERVER] = serpapi
        servers.append(_server(SERPAPI_SERVER, settings.serpapi_endpoint, settings))
        allowlist.extend(_entry(SERPAPI_SERVER, tool.name) for tool in SERPAPI_TOOLS)
    else:
        log.info("web.serpapi_absent", reason="no SERPAPI_KEY configured")

    return WebTools(
        servers=tuple(servers), allowlist=tuple(allowlist), providers=providers
    )


def _server(name: str, target: str, settings: WebToolsConfig) -> ServerConfig:
    return ServerConfig(
        name=name,
        # The local factory dispatches on the name; the target still names the
        # host this provider actually reaches, so the egress surface is
        # readable from the configuration rather than only from the code.
        target=target,
        timeout_seconds=settings.timeout_seconds + TIMEOUT_HEADROOM,
    )
