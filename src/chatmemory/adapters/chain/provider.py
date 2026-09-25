"""Wallet balances, as a local tool session behind the egress guard.

The clearance rule is the interesting decision, and it is `adapters.web`'s,
not `adapters.market`'s. A market lookup is checked by membership of a fixed
set; addresses have no fixed set to be a member of. A web query is checked by
rooting -- it must be made of the asker's own words -- and an address is a
word: `_WORD` is `[^\\W_]+`, so `0x742d…` tokenises whole and survives rooting
exactly when the person asking typed it.

That gives this feature the property it needs by construction. The assistant
can look up a wallet **you** typed and cannot look up one it read in a
channel, so it can never be turned into a way to sweep the addresses mentioned
across everything somebody can read.

Rooting is necessary and not sufficient: it admits any word the asker wrote.
So a structural gate follows it, and it refuses rather than trims. The web
path retries a not-rooted query with foreign words removed, which is right for
prose and catastrophic here -- an address trimmed to whichever part of it was
valid hex is a different, valid-looking address belonging to somebody else.

Order of checks, all before any request: the tool exists; the clearance exists
and is for this provider; the cleared text is rooted in the question; the
cleared text is an address; the budget and the rate limit. No failure escapes
as an exception, because `Federation.call` marks a session lost for the rest of
the run when one does.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

import structlog

from chatmemory.adapters.chain.clearance import clear_address, refusal
from chatmemory.adapters.chain.rpc import ChainBalances, ChainReader
from chatmemory.adapters.chain.tokens import tokens_for
from chatmemory.adapters.mcp_client.session import DiscoveredTool, ToolResult, ToolSession
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.app.authorization import ToolEffect
from chatmemory.app.currency import Conversion, UsdRates, asker_conversion, beside, rate_lines
from chatmemory.app.egress import CHAIN_BALANCES_PROVIDER

log = structlog.get_logger()

WALLET_TOOL = "wallet_balances"

WALLET_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": (
                    "The wallet address to look up, exactly as the person "
                    "wrote it. Must be a 0x-prefixed 40-character address."
                ),
            }
        },
        "required": ["address"],
        "additionalProperties": False,
    }
)

WALLET_DESCRIPTION = (
    "Current balances held by a wallet address on Ethereum, Base and Arbitrum: the "
    "native ETH balance and known tokens such as USDC, USDT, DAI and WETH. "
    "Use when someone gives a 0x address and asks what it holds, its balance, "
    "or how much is in it. Reports figures only."
)

#: What a dollar-pegged token is worth, without spending a price lookup on it.
PEGGED_USD = Decimal(1)


@dataclass(frozen=True, slots=True)
class PricedAsset:
    symbol: str
    usd: Decimal
    as_of: str


class PriceLookup(Protocol):
    """What the presenter needs of a price source. Absent is allowed.

    A Protocol rather than a base class: the source is an adapter detail, and
    a deployment that wants raw balances passes nothing rather than a subclass
    that returns None.
    """

    async def usd_price(self, symbol: str) -> PricedAsset | None:
        """The price, or None. Must not raise: a missing price omits a figure,
        it never fails a balance that has already been read."""
        ...


class WalletProvider:
    """Balances for one address across every configured chain."""

    server = CHAIN_BALANCES_PROVIDER

    def __init__(
        self,
        readers: Sequence[ChainReader],
        budget: CallBudget,
        limiter: RateLimiter | None = None,
        prices: PriceLookup | None = None,
        rates: UsdRates | None = None,
    ) -> None:
        if not readers:
            raise ValueError("a wallet provider needs at least one chain to read")
        self._readers = tuple(readers)
        self._budget = budget
        self._limiter = limiter or RateLimiter()
        self._prices = prices
        #: Where the asker's preferred currency is priced from; None adds nothing.
        self._rates = rates

    @asynccontextmanager
    async def opened(self) -> AsyncIterator[ToolSession]:
        yield self

    # --- ToolSession ---------------------------------------------------

    async def list_tools(self) -> Sequence[DiscoveredTool]:
        return [
            DiscoveredTool(
                name=WALLET_TOOL,
                description=WALLET_DESCRIPTION,
                effect=ToolEffect.READ_ONLY,
                input_schema=WALLET_SCHEMA,
            )
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        if name != WALLET_TOOL:
            return self._refuse(name, "unknown_tool")

        cleared = await clear_address(self.server, name, self._budget, self._limiter)
        if isinstance(cleared, ToolResult):
            return cleared
        address = cleared.address

        # Every chain at once. One being slow must not make the other late,
        # and each reader is already bounded and returns its failure as a
        # value rather than raising.
        results = await asyncio.gather(
            *(r.balances(address, tokens_for(r.chain)) for r in self._readers)
        )
        conversion = await asker_conversion(self._rates, cleared.question)
        return ToolResult(text=await self._render(address, results, conversion))

    # --- presentation --------------------------------------------------

    async def _render(
        self,
        address: str,
        results: Sequence[ChainBalances],
        conversion: Conversion | None = None,
    ) -> str:
        lines = [f"Balances for `{address}`"]
        for result in results:
            lines.append("")
            if not result.ok:
                # Named as unreachable, never rendered as empty: the two are
                # indistinguishable once they are prose, and only one of them
                # is a reason to worry.
                lines.append(f"**{result.chain.name}** — could not be read ({result.unreachable})")
                continue
            lines.append(f"**{result.chain.name}**")
            native = result.native or Decimal(0)
            lines.append(f"- {native:.6f} {result.chain.native_symbol}"
                         + await self._usd(result.chain.price_symbol, native, conversion))
            for holding in result.tokens:
                value = (
                    f" (~${holding.amount:,.2f}{beside(conversion, holding.amount, wrap=' · {}')})"
                    if holding.dollar_pegged
                    else await self._usd(holding.symbol, holding.amount, conversion)
                )
                lines.append(f"- {holding.amount:,.6f} {holding.symbol}{value}")
            if not result.tokens:
                lines.append("- no tokens held from the set this deployment tracks")
        lines.append("")
        lines.append(
            "Token balances cover a named set (USDC, USDT, DAI, WETH); anything "
            "else held is not visible to this lookup rather than absent."
        )
        return "\n".join([*lines, *rate_lines(conversion)])

    async def _usd(
        self, symbol: str, amount: Decimal, conversion: Conversion | None = None
    ) -> str:
        if self._prices is None or not symbol or amount <= 0:
            return ""
        priced = await self._prices.usd_price(symbol)
        if priced is None:
            return ""
        value = amount * priced.usd
        second = beside(conversion, value, wrap=" · {}")
        return f" (~${value:,.2f}{second} at {priced.as_of})"

    def _refuse(self, tool: str, reason: str) -> ToolResult:
        return refusal(self.server, tool, reason)


if TYPE_CHECKING:  # pragma: no cover - type-checking only

    def _provider_conforms(provider: WalletProvider) -> ToolSession:
        return provider
