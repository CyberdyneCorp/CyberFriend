"""Reading one wallet's activity on every chain: explorer rows, then the node.

The explorers are read at the same time: three hosts, each with its own rate
limit, and one page a chain answers a normal week. The node reads -- Aave's
tokens and prices -- stay one chain after another, as every other positions
read does, because concurrent Infura bursts are what made a chain come back
"could not be reached" in production.

Each chain's failure is a value, never an exception: a chain that could not be
read must say so, and must never read as a chain where nothing happened.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, TypeVar

import httpx
import structlog

from chatmemory.adapters.chain.activity import (
    ChainActivity,
    Recognised,
    assets_moved,
    judge,
    needs_selectors,
    transactions,
)
from chatmemory.adapters.chain.activity_explorer import (
    Rows,
    activity_rows,
    indexed_until,
    multicall_selectors,
)
from chatmemory.adapters.chain.positions import TokenInfo
from chatmemory.adapters.chain.tokens import tokens_for
from chatmemory.app.wallet_activity import ActivityWindow

if TYPE_CHECKING:  # pragma: no cover - a cycle at runtime, a type only here
    from chatmemory.adapters.chain.positions_provider import ChainPositions

log = structlog.get_logger()

T = TypeVar("T")

STALE_AFTER = timedelta(minutes=30)
"""How far behind the window's end an explorer may be before the answer says
so. Blocks index within seconds when an explorer is healthy."""


def _with_freshness(result: ChainActivity, rows: Rows, window: ActivityWindow) -> ChainActivity:
    """Mark a chain whose explorer stops short of the window's end."""
    head = rows.indexed_until
    if head is None or head >= window.end - STALE_AFTER:
        return result
    return replace(result, stale_until=head)


class ActivityReader:
    """One lookup's reads, bounded per chain."""

    def __init__(
        self,
        chains: Sequence[ChainPositions],
        client: httpx.AsyncClient,
        *,
        timeout_seconds: float,
        redact: Callable[[str], str] = lambda text: text,
    ) -> None:
        self._chains = tuple(chains)
        self._client = client
        self._timeout = timeout_seconds
        self._redact = redact

    async def read(
        self, address: str, window: ActivityWindow
    ) -> tuple[list[ChainActivity], dict[str, Recognised]]:
        """Every chain's activity, and what each chain's tokens were known as."""
        fetched = await asyncio.gather(
            *(self._bounded(c, self._rows(c, address, window)) for c in self._chains)
        )
        results: list[ChainActivity] = []
        known: dict[str, Recognised] = {}
        for chain, rows in zip(self._chains, fetched, strict=True):
            key = chain.deployment.chain.key
            if isinstance(rows, str):
                known[key] = _named_only(chain)
                results.append(ChainActivity(chain.deployment.chain, unreachable=rows))
                continue
            known[key] = await self._recognised(chain)
            judged = await self._judged(chain, address, rows, known[key])
            results.append(_with_freshness(judged, rows, window))
        return results, known

    async def _rows(self, chain: ChainPositions, address: str, window: ActivityWindow) -> Rows:
        explorer = chain.deployment.blockscout
        rows, head = await asyncio.gather(
            activity_rows(self._client, explorer, address, window.start, window.end),
            indexed_until(self._client, explorer),
        )
        return replace(rows, indexed_until=head)

    async def _recognised(self, chain: ChainPositions) -> Recognised:
        """The named tokens, Aave's reserves and its aTokens and debt tokens.

        Without Aave's tokens the answer still stands on the named ones, and
        says that Aave actions may be missing from it.
        """
        named = _named_only(chain)
        try:
            reserves = await asyncio.wait_for(chain.aave.reserve_tokens(), self._timeout)
            wrappers = await asyncio.wait_for(chain.aave.wrappers(), self._timeout)
        except Exception as exc:  # noqa: BLE001 - the rows are still worth showing
            log.warning("chain.activity_wrappers_failed", error=self._redact(str(exc))[:200])
            return _named_only(chain, wrappers_known=False)
        tokens = {**{t.address.lower(): t for t in reserves}, **named.tokens}
        return Recognised(
            tokens=tokens,
            wrappers=wrappers,
            position_managers=named.position_managers,
            weth=named.weth,
            native_symbol=named.native_symbol,
        )

    async def _judged(
        self, chain: ChainPositions, address: str, rows: Rows, known: Recognised
    ) -> ChainActivity:
        txs = transactions(rows.items, address, known)
        selectors: dict[str, frozenset[str]] = {}
        if needs_selectors(txs, known):
            selectors = await self._quietly(
                multicall_selectors(self._client, chain.deployment.blockscout, address), {}
            )
        prices = await self._prices(chain, assets_moved(txs, known) | {known.weth})
        judged = judge(txs, known, prices, selectors)
        return ChainActivity(
            chain=chain.deployment.chain,
            actions=judged.actions,
            gas=judged.gas,
            gas_transactions=judged.gas_transactions,
            relayed=judged.relayed,
            hidden=judged.hidden,
            truncated=rows.truncated,
            prices=prices,
            wrappers_known=known.wrappers_known,
        )

    async def _prices(self, chain: ChainPositions, assets: set[str]) -> dict[str, Decimal]:
        """Current USD prices from the chain's Aave oracle; none if it fails."""
        return await self._quietly(chain.aave.usd_prices(assets), {})

    async def _quietly(self, work: Awaitable[T], fallback: T) -> T:
        """A read that only adds detail: its failure leaves the detail out."""
        try:
            return await asyncio.wait_for(work, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - a price or a label never fails a chain
            log.info("chain.activity_detail_missing", error=self._redact(str(exc))[:200])
            return fallback

    async def _bounded(self, chain: ChainPositions, work: Awaitable[T]) -> T | str:
        """The chain's result, or why it failed as a value. Never raises."""
        key = chain.deployment.chain.key
        try:
            return await asyncio.wait_for(work, timeout=self._timeout)
        except TimeoutError:
            log.warning("chain.activity_timeout", chain=key, timeout=self._timeout)
            return "timed out"
        except Exception as exc:  # noqa: BLE001 - one chain must never fail the others
            log.warning("chain.activity_failed", chain=key, error=self._redact(str(exc))[:300])
            return "could not be reached"


def _named_only(chain: ChainPositions, *, wrappers_known: bool = True) -> Recognised:
    """What is known without the node: the named tokens and the managers."""
    deployment = chain.deployment
    tokens = {
        t.contract: TokenInfo(t.contract, t.symbol, t.decimals)
        for t in tokens_for(deployment.chain)
    }
    return Recognised(
        tokens=tokens,
        wrappers={},
        position_managers=frozenset(
            {deployment.v3_position_manager, deployment.v4_position_manager}
        ),
        weth=deployment.weth,
        native_symbol=deployment.chain.native_symbol,
        wrappers_known=wrappers_known,
    )
