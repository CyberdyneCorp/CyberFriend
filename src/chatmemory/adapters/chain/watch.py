"""Reads every due position alert in one `aggregate3` per chain.

The positions readers discover: balance, token by index, positions, pools,
simulated collects, an explorer and a log scan -- two to twenty-five requests
per chain per wallet. An alert already knows its position, so after creation
it needs only a few words: who owns the NFT now, its ticks and liquidity, and
the pool's current tick; or, for Aave, one `getUserAccountData`. Those are
folded into a single Multicall3 call per chain however many alerts are due,
which is about three requests a sweep for the alerts people actually keep.

Per alert, a fixed set of calls:

*   Uniswap v3 -- `ownerOf` and `positions` on the position manager, and
    `slot0` on the pool stored at creation.
*   Uniswap v4 -- `ownerOf`, `getPoolAndPositionInfo` and
    `getPositionLiquidity` on the position manager, and `getSlot0` on the
    StateView for the pool id stored at creation.
*   Aave v3 -- `getUserAccountData` on the pool, which is resolved once from
    the addresses provider and kept for the life of the process.

This is an egress path with no asker behind it. The address it sends was
cleared when the alert was made, and that is declared in the
`position-alerts` spec; this module never sees anything but a stored alert.
It has its own `RateLimiter`, so a sweep never spends the spacing interactive
lookups rely on, and a hard cap on calls per chain per sweep.

A chain that cannot be read is a `ReadFailure` for each of its alerts, never
an empty reading: "could not see it" must not become "it closed".
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping, Sequence

import httpx
import structlog

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.aave import (
    BASE_CURRENCY_DECIMALS,
    HEALTH_DECIMALS,
    NO_DEBT_HEALTH,
)
from chatmemory.adapters.chain.deployments import DEPLOYMENTS, Deployment
from chatmemory.adapters.chain.liquidity_math import price_at_tick, whole
from chatmemory.adapters.chain.node import DEFAULT_CHUNK, Call, Node
from chatmemory.adapters.chain.positions import TokenInfo
from chatmemory.adapters.chain.positions_render import orient
from chatmemory.adapters.web.limits import RateLimiter
from chatmemory.ports.alerts import (
    AlertKind,
    HealthObservation,
    LpObservation,
    LpProtocol,
    LpTarget,
    Observation,
    PositionAlert,
    ReadFailure,
)

log = structlog.get_logger()

DEFAULT_TIMEOUT = 20.0
"""Per chain. One multicall, so far more than it needs; bounded all the same."""

MAX_CHUNKS_PER_CHAIN = 10
MAX_CALLS_PER_CHAIN = MAX_CHUNKS_PER_CHAIN * DEFAULT_CHUNK
"""A sweep's hard ceiling per chain. Alerts past it are read next sweep."""

MIN_INTERVAL_SECONDS = 0.5
"""Spacing between this watcher's chain reads, on its own limiter."""


class ChainWatcher:
    """Implements `PositionObserver` over Infura, one multicall per chain."""

    def __init__(
        self,
        infura_key: str,
        deployments: Sequence[Deployment] = DEPLOYMENTS,
        *,
        limiter: RateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        endpoints: Mapping[str, str] | None = None,
        max_calls_per_chain: int = MAX_CALLS_PER_CHAIN,
    ) -> None:
        self._key = infura_key
        self._deployments = {d.chain.key: d for d in deployments}
        self._limiter = limiter or RateLimiter(MIN_INTERVAL_SECONDS)
        self._transport = transport
        self._timeout = timeout_seconds
        self._endpoints = dict(endpoints or {})
        self._max_calls = max_calls_per_chain
        self._aave_pools: dict[str, str] = {}

    async def observe(self, alerts: Sequence[PositionAlert]) -> Mapping[int, Observation]:
        by_chain: dict[str, list[PositionAlert]] = defaultdict(list)
        for alert in alerts:
            # A price alert has no chain, and is never handed here; if one
            # were, "" is a chain with no deployment, so a failed read.
            by_chain[alert.chain or ""].append(alert)
        out: dict[int, Observation] = {}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            # One chain after another, as the positions reader does: bursts
            # across chains are what Infura's per-second limit punishes.
            for chain, group in by_chain.items():
                out.update(await self._observe_chain(chain, group, client))
        return out

    async def _observe_chain(
        self, chain: str, alerts: list[PositionAlert], client: httpx.AsyncClient
    ) -> dict[int, Observation]:
        deployment = self._deployments.get(chain)
        if deployment is None:
            return _failed(alerts, "chain not configured")
        if not await self._limiter.acquire():
            return _failed(alerts, "rate limited")
        try:
            return await asyncio.wait_for(
                self._read(self._node(deployment, client), deployment, alerts),
                timeout=self._timeout,
            )
        except TimeoutError:
            log.warning("alerts.chain_timeout", chain=chain, timeout=self._timeout)
            return _failed(alerts, "timed out")
        except Exception as exc:  # noqa: BLE001 - one chain must never fail the others
            redacted = str(exc).replace(self._key, "***") if self._key else str(exc)
            log.warning("alerts.chain_failed", chain=chain, error=redacted[:300])
            return _failed(alerts, "could not be reached")

    def _node(self, deployment: Deployment, client: httpx.AsyncClient) -> Node:
        chain = deployment.chain
        endpoint = self._endpoints.get(chain.key) or (
            f"https://{chain.infura_host}.infura.io/v3/{self._key}"
        )
        return Node(endpoint, client, secret=self._key)

    async def _read(
        self, node: Node, deployment: Deployment, alerts: list[PositionAlert]
    ) -> dict[int, Observation]:
        pool = ""
        if any(a.kind is AlertKind.AAVE_HEALTH for a in alerts):
            pool = await self._aave_pool(node, deployment)
        planned: list[tuple[PositionAlert, list[Call]]] = []
        out: dict[int, Observation] = {}
        used = 0
        for alert in alerts:
            calls = calls_for(alert, deployment, pool)
            if not calls or used + len(calls) > self._max_calls:
                out[alert.id] = ReadFailure("not read this sweep")
                continue
            planned.append((alert, calls))
            used += len(calls)
        results = await node.multicall([c for _, calls in planned for c in calls])
        start = 0
        for alert, calls in planned:
            out[alert.id] = decode(alert, results[start : start + len(calls)])
            start += len(calls)
        return out

    async def _aave_pool(self, node: Node, deployment: Deployment) -> str:
        key = deployment.chain.key
        if key not in self._aave_pools:
            raw = await node.eth_call(deployment.aave_addresses_provider, abi.AAVE_GET_POOL)
            self._aave_pools[key] = abi.as_address(abi.words(raw)[0])
        return self._aave_pools[key]


def _failed(alerts: Sequence[PositionAlert], reason: str) -> dict[int, Observation]:
    return {a.id: ReadFailure(reason) for a in alerts}


# --- calls ----------------------------------------------------------------


def calls_for(alert: PositionAlert, deployment: Deployment, aave_pool: str) -> list[Call]:
    """The fixed calls one alert needs, in the order `decode` reads them."""
    if alert.address is None:
        return []
    if alert.kind is AlertKind.AAVE_HEALTH:
        return [Call(aave_pool, abi.call(abi.AAVE_ACCOUNT_DATA, abi.address(alert.address)))]
    lp = alert.lp
    if lp is None:
        return []
    token = abi.uint(lp.token_id)
    if lp.protocol is LpProtocol.UNISWAP_V3:
        manager = deployment.v3_position_manager
        return [
            Call(manager, abi.call(abi.OWNER_OF, token)),
            Call(manager, abi.call(abi.V3_POSITIONS, token)),
            Call(lp.pool_ref, abi.V3_SLOT0),
        ]
    manager = deployment.v4_position_manager
    return [
        Call(manager, abi.call(abi.OWNER_OF, token)),
        Call(manager, abi.call(abi.V4_POOL_AND_POSITION, token)),
        Call(manager, abi.call(abi.V4_POSITION_LIQUIDITY, token)),
        Call(deployment.v4_state_view, abi.call(abi.V4_SLOT0, lp.pool_ref.removeprefix("0x"))),
    ]


# --- decoding -------------------------------------------------------------


def decode(alert: PositionAlert, results: Sequence[bytes | None]) -> Observation:
    if alert.kind is AlertKind.AAVE_HEALTH:
        return health_reading(results[0])
    if alert.lp is None:
        return ReadFailure("no position stored")
    if alert.lp.protocol is LpProtocol.UNISWAP_V3:
        return _v3(alert, alert.lp, results)
    return _v4(alert, alert.lp, results)


def _owned_by(raw: bytes | None, address: str | None) -> bool:
    """A reverted `ownerOf` is a burned NFT: not owned by anybody."""
    if raw is None or address is None:
        return False
    return abi.as_address(abi.words(raw)[0]) == address.lower()


def _v3(alert: PositionAlert, lp: LpTarget, results: Sequence[bytes | None]) -> Observation:
    owner, position, slot0 = results
    if not _owned_by(owner, alert.address) or position is None:
        return LpObservation(owned=False)
    if slot0 is None:
        return ReadFailure("pool did not answer")
    w = abi.words(position)
    lower, upper = abi.signed(w[5], 24), abi.signed(w[6], 24)
    tick = abi.signed(abi.words(slot0)[1], 24)
    return lp_observation(lp, liquidity=w[7], tick=tick, lower=lower, upper=upper)


def _v4(alert: PositionAlert, lp: LpTarget, results: Sequence[bytes | None]) -> Observation:
    owner, info, liquidity, slot0 = results
    if not _owned_by(owner, alert.address) or info is None or liquidity is None:
        return LpObservation(owned=False)
    if slot0 is None:
        return ReadFailure("pool did not answer")
    packed = abi.words(info)[5]
    lower, upper = abi.signed(packed >> 8, 24), abi.signed(packed >> 32, 24)
    tick = abi.signed(abi.words(slot0)[1], 24)
    return lp_observation(
        lp, liquidity=abi.words(liquidity)[0], tick=tick, lower=lower, upper=upper
    )


def lp_observation(
    lp: LpTarget, *, liquidity: int, tick: int, lower: int, upper: int
) -> LpObservation:
    """A reading with its prices, quoted the way the positions answer quotes them."""
    t0 = TokenInfo("", lp.token0_symbol, lp.token0_decimals)
    t1 = TokenInfo("", lp.token1_symbol, lp.token1_decimals)
    base, quote, price, low, high = orient(
        t0,
        t1,
        price_at_tick(tick, t0.decimals, t1.decimals),
        price_at_tick(lower, t0.decimals, t1.decimals),
        price_at_tick(upper, t0.decimals, t1.decimals),
    )
    return LpObservation(
        owned=True,
        liquidity=liquidity,
        tick=tick,
        tick_lower=lower,
        tick_upper=upper,
        price=price,
        price_lower=low,
        price_upper=high,
        base_symbol=base.symbol,
        quote_symbol=quote.symbol,
    )


def health_reading(raw: bytes | None) -> Observation:
    if raw is None:
        return ReadFailure("Aave did not answer")
    collateral, debt, _, _, _, health = abi.words(raw)[:6]
    return HealthObservation(
        health_factor=None if health == NO_DEBT_HEALTH or not debt
        else whole(health, HEALTH_DECIMALS),
        collateral_usd=whole(collateral, BASE_CURRENCY_DECIMALS),
        debt_usd=whole(debt, BASE_CURRENCY_DECIMALS),
    )
