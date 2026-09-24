"""Reading a portfolio: every wallet, chain by chain, each section retried once.

One `Node` and one set of readers per chain for the whole lookup, however many
wallets it covers: the Aave contracts are resolved once, token symbols are read
once, and the rate limiter sees one steady stream per chain instead of three
readers bursting at it. Chains are read one after another, as positions are,
for the same reason.

A section that fails is tried once more before it is given up on. Infura's
rate limiter is the usual cause, and a moment later the same read answers.
Everything shares one deadline, so a slow chain costs the others their retry
rather than making the whole answer late.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, TypeVar

import structlog

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.deployments import NATIVE
from chatmemory.adapters.chain.liquidity_math import whole
from chatmemory.adapters.chain.node import Call
from chatmemory.adapters.chain.portfolio import (
    ChainPortfolio,
    ChainWallet,
    Held,
    WalletPortfolio,
)
from chatmemory.adapters.chain.positions import ChainLending, ChainLiquidity, TokenInfo
from chatmemory.adapters.chain.provider import PEGGED_USD, PriceLookup
from chatmemory.adapters.chain.tokens import tokens_for

if TYPE_CHECKING:  # pragma: no cover - the provider module imports this one
    from chatmemory.adapters.chain.positions_provider import ChainPositions

log = structlog.get_logger()

ATTEMPTS = 2
"""A section is read at most twice: once, and once more after a failure."""

T = TypeVar("T")


class PortfolioReader:
    """One portfolio lookup. Built per question, like `ChainPositions`."""

    def __init__(
        self,
        chains: Sequence[ChainPositions],
        *,
        timeout_seconds: float,
        budget_seconds: float,
        fallback_prices: PriceLookup | None = None,
        redact: Callable[[str], str] = lambda text: text,
    ) -> None:
        self._chains = tuple(chains)
        self._timeout = timeout_seconds
        self._deadline = _now() + budget_seconds
        self._fallback = fallback_prices
        self._redact = redact

    async def read(self, address: str) -> WalletPortfolio:
        chains = [await self._chain(c, address) for c in self._chains]
        return WalletPortfolio(address=address, chains=tuple(chains))

    async def _chain(self, chain: ChainPositions, address: str) -> ChainPortfolio:
        name = chain.deployment.chain
        wallet = await self._section(
            chain,
            lambda: self._wallet(chain, address),
            lambda reason: ChainWallet(name, unreachable=reason),
        )
        liquidity = await self._section(
            chain,
            lambda: chain.uniswap.liquidity(address),
            lambda reason: ChainLiquidity(name, unreachable=reason),
        )
        lending = await self._section(
            chain,
            lambda: chain.aave.lending(address),
            lambda reason: ChainLending(name, unreachable=reason),
        )
        return ChainPortfolio(wallet=wallet, liquidity=liquidity, lending=lending)

    async def _section(
        self,
        chain: ChainPositions,
        work: Callable[[], Awaitable[T]],
        failed: Callable[[str], T],
    ) -> T:
        """The section, or its failure as a value after a second try. Never raises."""
        reason = "timed out"
        for _ in range(ATTEMPTS):
            remaining = self._deadline - _now()
            if remaining <= 0:
                break
            try:
                return await asyncio.wait_for(work(), timeout=min(self._timeout, remaining))
            except TimeoutError:
                reason = "timed out"
            except Exception as exc:  # noqa: BLE001 - one section must never fail the others
                reason = "could not be reached"
                log.warning(
                    "chain.portfolio_section_failed",
                    chain=chain.deployment.chain.key,
                    error=self._redact(str(exc))[:300],
                )
        return failed(reason)

    # --- the wallet itself -------------------------------------------------

    async def _wallet(self, chain: ChainPositions, address: str) -> ChainWallet:
        candidates = _candidates(chain, await chain.aave.reserve_tokens())
        native = whole(await chain.node.native_balance(address), 18)
        results = await chain.node.multicall(
            [Call(t.address, abi.call(abi.BALANCE_OF, abi.address(address))) for t in candidates]
        )
        held = [
            (token, whole(raw, token.decimals))
            for token, result in zip(candidates, results, strict=True)
            if (raw := _first_word(result))
        ]
        prices, fallback = await self._prices(chain, {t.address for t, _ in held} | {NATIVE})
        holdings = [
            Held(t.symbol, amount, prices.get(t.address) or _pegged(t)) for t, amount in held
        ]
        if native:
            symbol = chain.deployment.chain.native_symbol
            holdings.insert(0, Held(symbol, native, prices.get(NATIVE)))
        return ChainWallet(
            chain=chain.deployment.chain, holdings=tuple(holdings), fallback_priced=fallback
        )

    async def _prices(
        self, chain: ChainPositions, assets: set[str]
    ) -> tuple[dict[str, Decimal], bool]:
        """The chain's Aave oracle, and CoinGecko for ether if it did not answer.

        One source per chain: the same asset priced twice in one answer, a
        tenth of a percent apart, reads as a mistake even when both are right.
        """
        try:
            prices = await chain.aave.usd_prices(assets)
        except Exception as exc:  # noqa: BLE001 - unpriced, never unread
            log.warning("chain.portfolio_prices_failed", error=self._redact(str(exc))[:300])
            prices = {}
        ether = {NATIVE, chain.deployment.weth.lower()} & assets
        unpriced = ether - prices.keys()
        if not unpriced or self._fallback is None:
            return prices, False
        quoted = await self._fallback.usd_price("ETH")
        if quoted is None:
            return prices, False
        return {**prices, **dict.fromkeys(unpriced, quoted.usd)}, True


def _candidates(chain: ChainPositions, reserves: Sequence[TokenInfo]) -> tuple[TokenInfo, ...]:
    """The named tokens, then every Aave reserve asset not already among them."""
    named = [
        TokenInfo(t.contract.lower(), t.symbol, t.decimals)
        for t in tokens_for(chain.deployment.chain)
    ]
    seen = {t.address for t in named}
    extra = [t for t in reserves if t.address.lower() not in seen]
    return (*named, *(TokenInfo(t.address.lower(), t.symbol, t.decimals) for t in extra))


def _first_word(result: bytes | None) -> int:
    """A `balanceOf` answer, or 0 when the call reverted or said nothing."""
    words = abi.words(result) if result else []
    return words[0] if words else 0


def _pegged(token: TokenInfo) -> Decimal | None:
    """A dollar stablecoin the oracle does not list is still worth a dollar."""
    return PEGGED_USD if token.is_stable else None


def _now() -> float:
    return asyncio.get_running_loop().time()
