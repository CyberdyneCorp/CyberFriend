"""Market data tools, assembled as federation configuration.

Deliberately the same shape as `adapters.web.registration`: a tuple of
`ServerConfig`, a tuple of `AllowedTool` and a `SessionFactory`, which are the
three things `connect()` takes. A price lookup is then registered, routed,
authorized at invoke time and audited by the code that governs every other
outbound call; nothing downstream knows these servers run in process.

Decided here and nowhere else:

*   **Read-only is our declaration.** The allowlist entry sets the effect, and
    it is the only place the registry accepts that determination from.
*   **The S&P 500 exists only with a SerpApi key.** No key, no server, no
    allowlist entry, nothing to route to.
*   **One budget for all market lookups.** The cap is on egress per question,
    not on any one vendor's quota.

Wiring into the running bot is `composition`'s job: merge `servers` and
`allowlist` into the federation config and chain `factory` in front of the
existing session factory, exactly as it already does for `build_web_tools`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace

import httpx
import structlog

from chatmemory.adapters.market.coingecko import (
    COINGECKO_ENDPOINT,
    CoinGeckoProvider,
)
from chatmemory.adapters.market.coingecko import (
    DEFAULT_TTL_SECONDS as CRYPTO_TTL_SECONDS,
)
from chatmemory.adapters.market.frankfurter import (
    DEFAULT_TTL_SECONDS as FX_TTL_SECONDS,
)
from chatmemory.adapters.market.frankfurter import (
    FRANKFURTER_ENDPOINT,
    FrankfurterProvider,
)
from chatmemory.adapters.market.google_finance import (
    DEFAULT_TTL_SECONDS as INDEX_TTL_SECONDS,
)
from chatmemory.adapters.market.google_finance import (
    SERPAPI_ENDPOINT,
    GoogleFinanceProvider,
)
from chatmemory.adapters.market.provider import DEFAULT_TIMEOUT, MarketProvider
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
from chatmemory.app.authorization import CredentialScope, ToolEffect
from chatmemory.app.egress import (
    MARKET_CRYPTO_PROVIDER,
    MARKET_FX_PROVIDER,
    MARKET_INDEX_PROVIDER,
)

log = structlog.get_logger()

MARKET_SERVERS = frozenset({MARKET_CRYPTO_PROVIDER, MARKET_FX_PROVIDER, MARKET_INDEX_PROVIDER})

TIMEOUT_HEADROOM = 2.0
"""The federation client waits longer than the provider, so a slow source
comes back as this package's own "unavailable" rather than being cut off."""


@dataclass(frozen=True, slots=True)
class MarketToolsConfig:
    """Everything an operator can set about market data."""

    serpapi_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT
    max_calls_per_run: int = DEFAULT_CALLS_PER_RUN
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL
    crypto_ttl_seconds: float = CRYPTO_TTL_SECONDS
    fx_ttl_seconds: float = FX_TTL_SECONDS
    index_ttl_seconds: float = INDEX_TTL_SECONDS
    coingecko_endpoint: str = COINGECKO_ENDPOINT
    frankfurter_endpoint: str = FRANKFURTER_ENDPOINT
    serpapi_endpoint: str = SERPAPI_ENDPOINT
    transport: httpx.AsyncBaseTransport | None = None
    """What every client this package opens sends through. None is httpx's
    own network transport; a test hands a mock here and nothing leaves."""


@dataclass(frozen=True, slots=True)
class MarketTools:
    """The market providers, ready to be handed to `connect()`."""

    servers: tuple[ServerConfig, ...] = ()
    allowlist: tuple[AllowedTool, ...] = ()
    providers: Mapping[str, MarketProvider] = field(default_factory=dict)

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.servers)

    def factory(self, fallback: SessionFactory | None = None) -> SessionFactory:
        """A session factory that opens these providers locally.

        Anything it does not recognise goes to `fallback`, so it chains in
        front of the web tools' factory and the remote MCP factory alike.
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

        Validated by `FederationConfig` itself, so an operator's MCP server
        that collides with a market server name is a startup error rather than
        a silent override.
        """
        return replace(
            config,
            servers=(*config.servers, *self.servers),
            allowlist=(*config.allowlist, *self.allowlist),
            _by_name={},
        )


def build_market_tools(
    config: MarketToolsConfig | None = None, *, client: httpx.AsyncClient | None = None
) -> MarketTools:
    """Assemble whichever market providers this deployment can run."""
    settings = config or MarketToolsConfig()
    budget = CallBudget(settings.max_calls_per_run)
    providers: list[tuple[MarketProvider, str]] = [
        (
            CoinGeckoProvider(
                budget,
                endpoint=settings.coingecko_endpoint,
                ttl_seconds=settings.crypto_ttl_seconds,
                limiter=RateLimiter(settings.min_interval_seconds),
                timeout_seconds=settings.timeout_seconds,
                client=client,
                transport=settings.transport,
            ),
            settings.coingecko_endpoint,
        ),
        (
            FrankfurterProvider(
                budget,
                endpoint=settings.frankfurter_endpoint,
                ttl_seconds=settings.fx_ttl_seconds,
                limiter=RateLimiter(settings.min_interval_seconds),
                timeout_seconds=settings.timeout_seconds,
                client=client,
                transport=settings.transport,
            ),
            settings.frankfurter_endpoint,
        ),
    ]
    key = (settings.serpapi_key or "").strip()
    if key:
        index = GoogleFinanceProvider(
            key,
            budget,
            endpoint=settings.serpapi_endpoint,
            ttl_seconds=settings.index_ttl_seconds,
            limiter=RateLimiter(settings.min_interval_seconds),
            timeout_seconds=settings.timeout_seconds,
            client=client,
            transport=settings.transport,
        )
        providers.append((index, settings.serpapi_endpoint))
    else:
        log.info("market.index_absent", reason="no SERPAPI_KEY configured")

    return MarketTools(
        servers=tuple(_server(p.server, target, settings) for p, target in providers),
        allowlist=tuple(_entry(p.server, p.tool.name) for p, _ in providers),
        providers={p.server: p for p, _ in providers},
    )


def _entry(server: str, tool: str) -> AllowedTool:
    return AllowedTool(
        server=server,
        tool=tool,
        # CoinGecko and Frankfurter have no credential; the SerpApi key
        # returns the same public figure to anyone holding it.
        credential=CredentialScope.NARROW_READ_ONLY,
        effect=ToolEffect.READ_ONLY,
        mutation_enabled=False,
    )


def _server(name: str, target: str, settings: MarketToolsConfig) -> ServerConfig:
    return ServerConfig(
        name=name,
        # The host this provider actually reaches, so the egress surface is
        # readable from configuration, not only from code.
        target=target,
        timeout_seconds=settings.timeout_seconds + TIMEOUT_HEADROOM,
    )
