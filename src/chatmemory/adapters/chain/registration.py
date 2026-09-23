"""Wallet balances, assembled as federation configuration.

The same shape as `adapters.web.registration` and `adapters.market`: servers,
an allowlist and a session factory, so the lookup is registered, routed,
authorized at invoke time and audited by the code that governs every other
outbound call.

Decided here and nowhere else:

*   **Read-only is our declaration.** The allowlist entry sets the effect, and
    it is the only place the registry accepts that determination from. There
    is no signer in this package, so the declaration and the capability agree.
*   **No key, no server.** Without an endpoint credential nothing is built,
    nothing is registered, and the model is never offered a tool it cannot
    use.
*   **One budget for all chains.** A wallet lookup reads every configured
    chain, and the cap is on lookups per question rather than per chain.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace

import httpx
import structlog

from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.chain.positions_provider import (
    DEFAULT_TIMEOUT as POSITIONS_TIMEOUT,
)
from chatmemory.adapters.chain.positions_provider import (
    TOOLS as POSITION_TOOLS,
)
from chatmemory.adapters.chain.positions_provider import (
    PositionsProvider,
)
from chatmemory.adapters.chain.prices import CoinGeckoPrices
from chatmemory.adapters.chain.provider import PriceLookup, WalletProvider
from chatmemory.adapters.chain.rpc import DEFAULT_TIMEOUT, ChainReader
from chatmemory.adapters.chain.tokens import CHAINS
from chatmemory.adapters.mcp_client.client import SessionFactory
from chatmemory.adapters.mcp_client.config import (
    AllowedTool,
    FederationConfig,
    ServerConfig,
)
from chatmemory.adapters.mcp_client.session import ToolSession, default_session_factory
from chatmemory.adapters.web.limits import (
    DEFAULT_MIN_INTERVAL,
    CallBudget,
    RateLimiter,
)
from chatmemory.app.authorization import CredentialScope, ToolEffect

log = structlog.get_logger()

CHAIN_SERVER = WalletProvider.server
POSITIONS_SERVER = PositionsProvider.server

TIMEOUT_HEADROOM = 2.0
"""The federation client waits longer than the reader, so a slow chain comes
back as this package's own "could not be read" rather than being cut off."""

DEFAULT_CALLS_PER_RUN = 2
"""A wallet lookup reads every chain at once, so one question rarely needs
more than one; two leaves room for a follow-up about a second address."""


@dataclass(frozen=True, slots=True)
class ChainToolsConfig:
    """Everything an operator can set about wallet balances."""

    infura_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT
    max_calls_per_run: int = DEFAULT_CALLS_PER_RUN
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL
    positions_timeout_seconds: float = POSITIONS_TIMEOUT


@dataclass(frozen=True, slots=True)
class ChainTools:
    """The wallet provider, ready to be handed to `connect()`."""

    servers: tuple[ServerConfig, ...] = ()
    allowlist: tuple[AllowedTool, ...] = ()
    providers: Mapping[str, WalletProvider | PositionsProvider] = field(default_factory=dict)

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.servers)

    def factory(self, fallback: SessionFactory | None = None) -> SessionFactory:
        """A session factory that opens this provider locally."""
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
        return replace(
            config,
            servers=(*config.servers, *self.servers),
            allowlist=(*config.allowlist, *self.allowlist),
            _by_name={},
        )


def build_chain_tools(
    config: ChainToolsConfig | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    prices: PriceLookup | None = None,
) -> ChainTools:
    """Assemble the wallet provider, or nothing when no endpoint is configured."""
    settings = config or ChainToolsConfig()
    key = (settings.infura_key or "").strip()
    if not key:
        log.info("chain.absent", reason="no INFURA_KEY configured")
        return ChainTools()

    readers = [
        ChainReader(
            chain,
            key,
            client=client,
            timeout_seconds=settings.timeout_seconds,
        )
        for chain in CHAINS
    ]
    provider = WalletProvider(
        readers,
        CallBudget(settings.max_calls_per_run),
        RateLimiter(settings.min_interval_seconds),
        # Default rather than required: a deployment that wants raw balances
        # passes its own, and one that passes nothing still gets USD values.
        prices=prices or CoinGeckoPrices(client=client, timeout_seconds=settings.timeout_seconds),
    )
    positions = PositionsProvider(
        DEPLOYMENTS,
        key,
        CallBudget(settings.max_calls_per_run),
        RateLimiter(settings.min_interval_seconds),
        client=client,
        timeout_seconds=settings.positions_timeout_seconds,
    )
    return ChainTools(
        servers=(
            ServerConfig(
                name=CHAIN_SERVER,
                # The hosts this provider actually reaches, so the egress
                # surface is readable from configuration and not only code.
                target=",".join(f"https://{c.infura_host}.infura.io" for c in CHAINS),
                timeout_seconds=settings.timeout_seconds + TIMEOUT_HEADROOM,
            ),
            ServerConfig(
                name=POSITIONS_SERVER,
                # Blockscout is reached only to find v4 position IDs; every
                # figure is read from the chain.
                target=",".join(
                    [f"https://{d.chain.infura_host}.infura.io" for d in DEPLOYMENTS]
                    + [d.blockscout for d in DEPLOYMENTS]
                ),
                # The combined tool reads liquidity, then lending, each bounded
                # per chain.
                timeout_seconds=2 * settings.positions_timeout_seconds + TIMEOUT_HEADROOM,
            ),
        ),
        allowlist=(
            AllowedTool(
                server=CHAIN_SERVER,
                tool="wallet_balances",
                # The Infura key reads public chain state and returns the same
                # figures to anyone holding it; there is nothing a requester
                # could borrow through it.
                credential=CredentialScope.NARROW_READ_ONLY,
                effect=ToolEffect.READ_ONLY,
                mutation_enabled=False,
            ),
            *(
                AllowedTool(
                    server=POSITIONS_SERVER,
                    tool=tool,
                    # Public chain state and a public explorer; `collect` is
                    # simulated with `eth_call` and never sent.
                    credential=CredentialScope.NARROW_READ_ONLY,
                    effect=ToolEffect.READ_ONLY,
                    mutation_enabled=False,
                )
                for tool in POSITION_TOOLS
            ),
        ),
        providers={CHAIN_SERVER: provider, POSITIONS_SERVER: positions},
    )
