"""Liquidity and lending positions, as a local tool session behind the guard.

The same clearance as wallet balances (`clearance.clear_address`): the address
must be one the asker typed or saved about themselves, it is refused rather
than trimmed, and every call is budgeted and rate limited.

Three tools rather than one with a "kind" argument. The route decides which
kind a question is about and offers exactly one of them, so the model's only
job is to copy the address -- it never chooses what gets read.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeVar

import httpx
import structlog

from chatmemory.adapters.chain.aave import AaveReader
from chatmemory.adapters.chain.clearance import clear_address, refusal
from chatmemory.adapters.chain.deployments import Deployment
from chatmemory.adapters.chain.node import Node
from chatmemory.adapters.chain.positions import ChainLending, ChainLiquidity, TokenDirectory
from chatmemory.adapters.chain.positions_render import render_lending, render_liquidity
from chatmemory.adapters.chain.uniswap import UniswapReader
from chatmemory.adapters.mcp_client.session import DiscoveredTool, ToolResult, ToolSession
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.app.authorization import ToolEffect
from chatmemory.app.egress import DEFI_POSITIONS_PROVIDER

log = structlog.get_logger()

LIQUIDITY_TOOL = "liquidity_positions"
LENDING_TOOL = "lending_positions"
ALL_POSITIONS_TOOL = "defi_positions"
TOOLS = (LIQUIDITY_TOOL, LENDING_TOOL, ALL_POSITIONS_TOOL)

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
}

T = TypeVar("T")


class ChainPositions:
    """One chain's readers, built per lookup so nothing is shared between askers."""

    def __init__(self, deployment: Deployment, node: Node, explorer: httpx.AsyncClient) -> None:
        tokens = TokenDirectory(node)
        self.deployment = deployment
        self.aave = AaveReader(node, deployment, tokens)
        self.uniswap = UniswapReader(node, deployment, tokens, self.aave, explorer)


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
                input_schema=_SCHEMA,
            )
            for name in TOOLS
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        if name not in TOOLS:
            return refusal(self.server, name, "unknown_tool")
        cleared = await clear_address(
            self.server, name, self._budget, self._limiter, per_tool_budget=True
        )
        if isinstance(cleared, ToolResult):
            return cleared
        return ToolResult(text=await self.report(name, cleared.address))

    # --- reading -------------------------------------------------------

    async def report(self, tool: str, address: str) -> str:
        """The rendered answer. Assumes the address is already cleared."""
        if self._client is not None:
            return await self._report(tool, address, self._client)
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
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
            redacted = str(exc).replace(self._key, "***") if self._key else str(exc)
            log.warning("chain.positions_failed", chain=key, error=redacted[:300])
            return failed("could not be reached")


if TYPE_CHECKING:  # pragma: no cover - type-checking only

    def _provider_conforms(provider: PositionsProvider) -> ToolSession:
        return provider
