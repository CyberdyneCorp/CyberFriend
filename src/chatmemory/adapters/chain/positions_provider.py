"""Liquidity and lending positions, as a local tool session behind the guard.

The same clearance as wallet balances (`clearance.clear_address`): the address
must be one the asker typed or saved about themselves, it is refused rather
than trimmed, and every call is budgeted and rate limited.

Three tools rather than one with a "kind" argument. The route decides which
kind a question is about and offers exactly one of them, so the model's only
job is to copy the address -- it never chooses what gets read.

A fourth, `portfolio_summary`, adds up everything: wallet balances, positions
and the Aave net, per chain and per wallet. It lives here rather than beside
wallet balances because it reads with these readers, sharing one node per
chain with them; its clearance is the same, extended to the asker's few own
wallets (`clearance.clear_addresses`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeVar

import httpx
import structlog

from chatmemory.adapters.chain.aave import AaveReader, ReserveCache
from chatmemory.adapters.chain.clearance import (
    MAX_ADDRESSES,
    Cleared,
    clear_address,
    clear_addresses,
    refusal,
)
from chatmemory.adapters.chain.deployments import Deployment
from chatmemory.adapters.chain.node import Node
from chatmemory.adapters.chain.portfolio_reader import PortfolioReader
from chatmemory.adapters.chain.portfolio_render import portfolio_language, render_portfolio
from chatmemory.adapters.chain.positions import ChainLending, ChainLiquidity, TokenDirectory
from chatmemory.adapters.chain.positions_render import render_lending, render_liquidity
from chatmemory.adapters.chain.provider import PriceLookup
from chatmemory.adapters.chain.uniswap import UniswapReader
from chatmemory.adapters.mcp_client.session import DiscoveredTool, ToolResult, ToolSession
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.app.authorization import ToolEffect
from chatmemory.app.egress import DEFI_POSITIONS_PROVIDER

log = structlog.get_logger()

LIQUIDITY_TOOL = "liquidity_positions"
LENDING_TOOL = "lending_positions"
ALL_POSITIONS_TOOL = "defi_positions"
PORTFOLIO_TOOL = "portfolio_summary"
TOOLS = (LIQUIDITY_TOOL, LENDING_TOOL, ALL_POSITIONS_TOOL, PORTFOLIO_TOOL)

DEFAULT_TIMEOUT = 25.0
"""Per chain; chains are read one after another. A wallet with a hundred
withdrawn position NFTs is still several multicalls (each must be read to know
it is empty), plus one simulated `collect` per open position."""

_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": (
                    "The wallet address, exactly as the person wrote it. "
                    "Must be a 0x-prefixed 40-character address."
                ),
            }
        },
        "required": ["address"],
        "additionalProperties": False,
    }
)

_PORTFOLIO_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "addresses": {
                "type": "string",
                "description": (
                    "Every wallet address in the question, exactly as written, "
                    f"separated by spaces (at most {MAX_ADDRESSES}). Each must be a "
                    "0x-prefixed 40-character address."
                ),
            }
        },
        "required": ["addresses"],
        "additionalProperties": False,
    }
)

_DESCRIPTIONS = {
    LIQUIDITY_TOOL: (
        "Open Uniswap v3 and v4 liquidity positions (closed ones are left out) "
        "of a wallet on Ethereum, "
        "Base and Arbitrum: pair, fee tier, amounts and USD value, in or out "
        "of range, min/max price and uncollected fees."
    ),
    LENDING_TOOL: (
        "Aave v3 positions of a wallet on Ethereum, Base and Arbitrum: supplied "
        "and borrowed assets with USD values and APYs, collateral, debt and "
        "health factor."
    ),
    ALL_POSITIONS_TOOL: (
        "Both the Uniswap liquidity positions and the Aave supplies and "
        "borrows of a wallet on Ethereum, Base and Arbitrum."
    ),
    PORTFOLIO_TOOL: (
        "Portfolio total: what one or more wallets are worth in total on "
        "Ethereum, Base and Arbitrum -- balances, Uniswap positions with "
        "uncollected fees and the Aave net, per chain and per wallet, with a "
        "grand total in USD (net worth, patrimônio, quanto tenho no total)."
    ),
}

T = TypeVar("T")


def portfolio_budget(chains: int, timeout_seconds: float) -> float:
    """The whole portfolio lookup's deadline, however many wallets it covers.

    Room for the three sections of every chain once, as the combined positions
    tool has for two. Retries and further wallets spend what the first pass
    left; a section still unread at the deadline is reported as not read, so
    the answer says "at least" rather than arriving after Discord gave up.
    """
    return 3 * chains * timeout_seconds


class ChainPositions:
    """One chain's readers, built per lookup so nothing is shared between askers.

    The reserve cache is the exception, and it holds nothing about anyone: see
    `aave.ReserveCache`.
    """

    def __init__(
        self,
        deployment: Deployment,
        node: Node,
        explorer: httpx.AsyncClient,
        reserves: ReserveCache | None = None,
    ) -> None:
        self.node = node
        self.tokens = TokenDirectory(node)
        self.deployment = deployment
        self.aave = AaveReader(node, deployment, self.tokens, reserves)
        self.uniswap = UniswapReader(node, deployment, self.tokens, self.aave, explorer)


class PositionsProvider:
    """Positions for one address across every configured chain."""

    server = DEFI_POSITIONS_PROVIDER

    def __init__(
        self,
        deployments: Sequence[Deployment],
        infura_key: str,
        budget: CallBudget,
        limiter: RateLimiter | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        endpoints: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        prices: PriceLookup | None = None,
    ) -> None:
        if not deployments:
            raise ValueError("a positions provider needs at least one chain to read")
        self._deployments = tuple(deployments)
        self._key = infura_key
        self._budget = budget
        self._limiter = limiter or RateLimiter()
        self._client = client
        self._timeout = timeout_seconds
        self._endpoints = dict(endpoints or {})
        self._transport = transport
        #: Ether's price when an Aave oracle does not answer; portfolio only.
        self._prices = prices
        self._reserves = ReserveCache()

    @asynccontextmanager
    async def opened(self) -> AsyncIterator[ToolSession]:
        yield self

    # --- ToolSession ---------------------------------------------------

    async def list_tools(self) -> Sequence[DiscoveredTool]:
        return [
            DiscoveredTool(
                name=name,
                description=_DESCRIPTIONS[name],
                effect=ToolEffect.READ_ONLY,
                input_schema=_PORTFOLIO_SCHEMA if name == PORTFOLIO_TOOL else _SCHEMA,
            )
            for name in TOOLS
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        if name not in TOOLS:
            return refusal(self.server, name, "unknown_tool")
        if name == PORTFOLIO_TOOL:
            return await self._portfolio_call()
        cleared = await clear_address(
            self.server, name, self._budget, self._limiter, per_tool_budget=True
        )
        if isinstance(cleared, ToolResult):
            return cleared
        return ToolResult(text=await self.report(name, cleared.address))

    async def _portfolio_call(self) -> ToolResult:
        cleared = await clear_addresses(
            self.server, PORTFOLIO_TOOL, self._budget, self._limiter
        )
        if isinstance(cleared, ToolResult):
            return cleared
        return ToolResult(text=await self.portfolio(cleared))

    # --- reading -------------------------------------------------------

    async def report(self, tool: str, address: str) -> str:
        """The rendered answer. Assumes the address is already cleared."""
        if self._client is not None:
            return await self._report(tool, address, self._client)
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            return await self._report(tool, address, client)

    async def _report(self, tool: str, address: str, client: httpx.AsyncClient) -> str:
        chains = [ChainPositions(d, self._node(d, client), client) for d in self._deployments]
        parts: list[str] = []
        # One chain at a time, not concurrently. Concurrent reads of three
        # chains, with the v4 recent-block search, burst past Infura's
        # per-second limit in production and Arbitrum came back "could not be
        # reached". A few seconds slower beats a chain reported unreadable.
        if tool in (LIQUIDITY_TOOL, ALL_POSITIONS_TOOL):
            liquidity = [await self._liquidity(c, address) for c in chains]
            parts.append(render_liquidity(address, liquidity))
        if tool in (LENDING_TOOL, ALL_POSITIONS_TOOL):
            lending = [await self._lending(c, address) for c in chains]
            text = render_lending(address, lending)
            # One header for the whole answer: `verbatim_answer` drops the
            # first line, and a second header mid-answer would read as noise.
            parts.append(text if not parts else "\n".join(text.splitlines()[1:]))
        return "\n".join(parts)

    async def portfolio(self, cleared: Cleared) -> str:
        """Every cleared wallet, summed. Assumes the addresses are cleared."""
        if self._client is not None:
            return await self._portfolio(cleared, self._client)
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            return await self._portfolio(cleared, client)

    async def _portfolio(self, cleared: Cleared, client: httpx.AsyncClient) -> str:
        chains = [
            ChainPositions(d, self._node(d, client), client, self._reserves)
            for d in self._deployments
        ]
        reader = PortfolioReader(
            chains,
            timeout_seconds=self._timeout,
            budget_seconds=portfolio_budget(len(self._deployments), self._timeout),
            fallback_prices=self._prices,
            redact=self._redact,
        )
        wallets = [await reader.read(address) for address in cleared.addresses]
        return render_portfolio(wallets, portfolio_language(cleared.question))

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def _node(self, deployment: Deployment, client: httpx.AsyncClient) -> Node:
        chain = deployment.chain
        endpoint = self._endpoints.get(chain.key) or (
            f"https://{chain.infura_host}.infura.io/v3/{self._key}"
        )
        return Node(endpoint, client, secret=self._key)

    async def _liquidity(self, chain: ChainPositions, address: str) -> ChainLiquidity:
        return await self._bounded(
            chain,
            chain.uniswap.liquidity(address),
            lambda reason: ChainLiquidity(chain.deployment.chain, unreachable=reason),
        )

    async def _lending(self, chain: ChainPositions, address: str) -> ChainLending:
        return await self._bounded(
            chain,
            chain.aave.lending(address),
            lambda reason: ChainLending(chain.deployment.chain, unreachable=reason),
        )

    async def _bounded(
        self, chain: ChainPositions, work: Awaitable[T], failed: Callable[[str], T]
    ) -> T:
        """The chain's result, or its failure as a value. Never raises."""
        key = chain.deployment.chain.key
        try:
            return await asyncio.wait_for(work, timeout=self._timeout)
        except TimeoutError:
            log.warning("chain.positions_timeout", chain=key, timeout=self._timeout)
            return failed("timed out")
        except Exception as exc:  # noqa: BLE001 - one chain must never fail the others
            log.warning("chain.positions_failed", chain=key, error=self._redact(str(exc))[:300])
            return failed("could not be reached")


if TYPE_CHECKING:  # pragma: no cover - type-checking only

    def _provider_conforms(provider: PositionsProvider) -> ToolSession:
        return provider
