"""What an alert could watch, read once when somebody asks for one.

Creation is the one time an alert discovers anything. A range alert runs the
same `UniswapReader` the positions answer runs -- balance, token by index,
positions, pools, the v4 explorer and log scan -- and keeps what the sweep
needs so it never has to again: the pool address or pool id, the symbols,
decimals and fee. The baseline is taken through `watch.lp_observation`, the
function the sweep reads with, so "in range" at creation and "in range" five
minutes later are the same test. A health alert needs only the account data,
decoded by the sweep's own `health_reading`.

Like the watcher, this sends an address with no per-call `clear_address`
behind it. The caller, `AlertRequests.propose`, only hands over an address the
asker typed or saved; this module never sees anything else, and it only runs
when alerts are switched on.

Chains are read one after another, as the positions answer reads them, and a
chain that fails is named rather than reported empty.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from typing import TypeVar

import httpx
import structlog

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.aave import AaveReader
from chatmemory.adapters.chain.deployments import DEPLOYMENTS, Deployment
from chatmemory.adapters.chain.node import Node
from chatmemory.adapters.chain.positions import LiquidityPosition, TokenDirectory
from chatmemory.adapters.chain.uniswap import UniswapReader
from chatmemory.adapters.chain.watch import health_reading, lp_observation
from chatmemory.adapters.web.limits import RateLimiter
from chatmemory.ports.alerts import (
    AlertKind,
    HealthCandidate,
    HealthObservation,
    LpCandidate,
    LpProtocol,
    LpTarget,
    TargetsRead,
)

log = structlog.get_logger()

DEFAULT_TIMEOUT = 25.0
"""Per chain, as for the positions answer: Arbitrum v4 took 7.4s live."""

T = TypeVar("T")

_PROTOCOLS = {"Uniswap v3": LpProtocol.UNISWAP_V3, "Uniswap v4": LpProtocol.UNISWAP_V4}


class ChainTargets:
    """Implements `AlertTargets` over Infura."""

    def __init__(
        self,
        infura_key: str,
        deployments: Sequence[Deployment] = DEPLOYMENTS,
        *,
        limiter: RateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        endpoints: Mapping[str, str] | None = None,
    ) -> None:
        self._key = infura_key
        self._deployments = tuple(deployments)
        self._limiter = limiter or RateLimiter()
        self._transport = transport
        self._timeout = timeout_seconds
        self._endpoints = dict(endpoints or {})

    async def read(self, address: str, kind: AlertKind, chain: str | None) -> TargetsRead:
        chosen = [d for d in self._deployments if chain is None or d.chain.key == chain]
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            parts = [await self._read_chain(kind, d, client, address) for d in chosen]
        return TargetsRead(
            lp=tuple(c for p in parts for c in p.lp),
            health=tuple(c for p in parts for c in p.health),
            unreachable=tuple(k for p in parts for k in p.unreachable),
            incomplete=tuple(k for p in parts for k in p.incomplete),
        )

    async def _read_chain(
        self, kind: AlertKind, deployment: Deployment, client: httpx.AsyncClient, address: str
    ) -> TargetsRead:
        """One chain's part of the read. A failure is this chain's, as a value."""
        key = deployment.chain.key
        try:
            if not await self._limiter.acquire():
                raise _Unreadable
            if kind is AlertKind.LP_RANGE:
                found, partial = await self._bounded(
                    self._positions(deployment, client, address), key
                )
                return TargetsRead(lp=tuple(found), incomplete=(key,) if partial else ())
            account = await self._bounded(self._account(deployment, client, address), key)
            return TargetsRead(health=(account,))
        except _Unreadable:
            return TargetsRead(unreachable=(key,))

    async def _bounded(self, work: Awaitable[T], chain: str) -> T:
        """The chain's result, or `_Unreadable`. Never another exception."""
        try:
            return await asyncio.wait_for(work, timeout=self._timeout)
        except TimeoutError as exc:
            log.warning("alerts.targets_timeout", chain=chain, timeout=self._timeout)
            raise _Unreadable from exc
        except Exception as exc:  # noqa: BLE001 - one chain must never fail the others
            redacted = str(exc).replace(self._key, "***") if self._key else str(exc)
            log.warning("alerts.targets_failed", chain=chain, error=redacted[:300])
            raise _Unreadable from exc

    def _node(self, deployment: Deployment, client: httpx.AsyncClient) -> Node:
        chain = deployment.chain
        endpoint = self._endpoints.get(chain.key) or (
            f"https://{chain.infura_host}.infura.io/v3/{self._key}"
        )
        return Node(endpoint, client, secret=self._key)

    async def _positions(
        self, deployment: Deployment, client: httpx.AsyncClient, address: str
    ) -> tuple[list[LpCandidate], bool]:
        node = self._node(deployment, client)
        tokens = TokenDirectory(node)
        reader = UniswapReader(
            node, deployment, tokens, AaveReader(node, deployment, tokens), client
        )
        found = await reader.liquidity(address)
        candidates = [
            c for p in found.positions if (c := _candidate(deployment.chain.key, p)) is not None
        ]
        return candidates, bool(found.notes)

    async def _account(
        self, deployment: Deployment, client: httpx.AsyncClient, address: str
    ) -> HealthCandidate:
        node = self._node(deployment, client)
        pool = abi.as_address(
            abi.words(await node.eth_call(deployment.aave_addresses_provider, abi.AAVE_GET_POOL))[0]
        )
        raw = await node.eth_call(pool, abi.call(abi.AAVE_ACCOUNT_DATA, abi.address(address)))
        reading = health_reading(raw)
        if not isinstance(reading, HealthObservation):
            raise _Unreadable
        return HealthCandidate(chain=deployment.chain.key, health_factor=reading.health_factor)


class _Unreadable(Exception):
    """A chain that could not be read. Named in the result, never raised past it."""


def _candidate(chain: str, position: LiquidityPosition) -> LpCandidate | None:
    """The alert-ready form of one open position, or None if it cannot be pinned."""
    protocol = _PROTOCOLS.get(position.protocol)
    if protocol is None or not position.pool_ref:
        return None
    target = LpTarget(
        protocol=protocol,
        token_id=position.token_id,
        pool_ref=position.pool_ref,
        token0_symbol=position.token0.symbol,
        token1_symbol=position.token1.symbol,
        token0_decimals=position.token0.decimals,
        token1_decimals=position.token1.decimals,
        fee=position.fee,
    )
    reading = lp_observation(
        target,
        liquidity=position.liquidity,
        tick=position.tick,
        lower=position.tick_lower,
        upper=position.tick_upper,
    )
    return LpCandidate(
        chain=chain,
        target=target,
        state=reading.state,
        tick=position.tick,
        price=reading.price,
        price_lower=reading.price_lower,
        price_upper=reading.price_upper,
        base_symbol=reading.base_symbol,
        quote_symbol=reading.quote_symbol,
    )
