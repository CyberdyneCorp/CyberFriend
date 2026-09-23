"""Uniswap v3 and v4 liquidity positions for one address on one chain.

v3 positions are listed by the chain itself. v4's position manager cannot
list an owner's tokens, so their IDs come from a Blockscout explorer -- and
only the IDs: each is confirmed with `ownerOf`, and every figure is read from
the chain. An explorer that is down or wrong can hide a v4 position (and the
answer then says how many could not be listed); it cannot add one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

import httpx
import structlog

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.aave import AaveReader
from chatmemory.adapters.chain.deployments import Deployment
from chatmemory.adapters.chain.liquidity_math import (
    amounts,
    fees_owed,
    price_at_tick,
    price_from_sqrt,
    whole,
)
from chatmemory.adapters.chain.node import Call, Node
from chatmemory.adapters.chain.positions import (
    ChainLiquidity,
    LiquidityPosition,
    TokenDirectory,
    TokenInfo,
)

log = structlog.get_logger()

MAX_POSITIONS = 400
"""Position NFTs read per protocol per chain. A wallet past this is a vault or
a bot, and the answer says the list was cut rather than pretending it is all."""

EXPLORER_PAGES = 4


@dataclass(frozen=True, slots=True)
class RawPosition:
    """A position as the contracts describe it, before tokens and prices."""

    protocol: str
    token_id: int
    token0: str
    token1: str
    fee: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    sqrt_price_x96: int = 0
    tick: int = 0
    owed0: int = 0
    owed1: int = 0


class UniswapReader:
    def __init__(
        self,
        node: Node,
        deployment: Deployment,
        tokens: TokenDirectory,
        prices: AaveReader,
        explorer: httpx.AsyncClient,
    ) -> None:
        self._node = node
        self._d = deployment
        self._tokens = tokens
        self._prices = prices
        self._explorer = explorer

    async def liquidity(self, owner: str) -> ChainLiquidity:
        notes: list[str] = []
        v3, v3_closed = await self._v3(owner, notes)
        v4, v4_closed = await self._v4(owner, notes)
        raw = v3 + v4
        positions = await self._present(raw) if raw else ()
        return ChainLiquidity(
            chain=self._d.chain,
            positions=positions,
            closed=v3_closed + v4_closed,
            notes=tuple(notes),
        )

    # --- v3 --------------------------------------------------------------

    async def _v3(self, owner: str, notes: list[str]) -> tuple[list[RawPosition], int]:
        manager = self._d.v3_position_manager
        count = await self._count(manager, owner, "Uniswap v3", notes)
        if not count:
            return [], 0
        indexes = await self._node.multicall([
            Call(manager, abi.call(abi.TOKEN_OF_OWNER_BY_INDEX, abi.address(owner), abi.uint(i)))
            for i in range(count)
        ])
        ids = [abi.words(r)[0] for r in indexes if r]
        details = await self._node.multicall(
            [Call(manager, abi.call(abi.V3_POSITIONS, abi.uint(t))) for t in ids]
        )
        positions: list[RawPosition] = []
        closed = 0
        for token_id, raw in zip(ids, details, strict=True):
            if not raw:
                continue
            w = abi.words(raw)
            position = RawPosition(
                protocol="Uniswap v3",
                token_id=token_id,
                token0=abi.as_address(w[2]),
                token1=abi.as_address(w[3]),
                fee=w[4],
                tick_lower=abi.signed(w[5], 24),
                tick_upper=abi.signed(w[6], 24),
                liquidity=w[7],
                owed0=w[10],
                owed1=w[11],
            )
            if position.liquidity or position.owed0 or position.owed1:
                positions.append(position)
            else:
                closed += 1
        positions = await self._v3_pool_state(positions)
        return await self._v3_fees(owner, positions), closed

    async def _v3_pool_state(self, positions: list[RawPosition]) -> list[RawPosition]:
        if not positions:
            return []
        factory = self._d.v3_factory
        keys = sorted({(p.token0, p.token1, p.fee) for p in positions})
        pools = await self._node.multicall([
            Call(factory, abi.call(abi.V3_GET_POOL, abi.address(t0), abi.address(t1), abi.uint(f)))
            for t0, t1, f in keys
        ])
        pool_of = {
            k: abi.as_address(abi.words(r)[0]) for k, r in zip(keys, pools, strict=True) if r
        }
        slots = await self._node.multicall([Call(pool_of[k], abi.V3_SLOT0) for k in pool_of])
        state = {
            k: (abi.words(r)[0], abi.signed(abi.words(r)[1], 24))
            for k, r in zip(pool_of, slots, strict=True)
            if r
        }
        out: list[RawPosition] = []
        for p in positions:
            sqrt_price, tick = state.get((p.token0, p.token1, p.fee), (0, 0))
            out.append(replace(p, sqrt_price_x96=sqrt_price, tick=tick))
        return out

    async def _v3_fees(self, owner: str, positions: list[RawPosition]) -> list[RawPosition]:
        """Uncollected fees, by simulating `collect` as the owner.

        One `eth_call` each: the manager only lets the owner collect, and a
        multicall's sender is the multicall contract. For a closed position
        `tokensOwed` is already exact and nothing is simulated.
        """
        out: list[RawPosition] = []
        for p in positions:
            if not p.liquidity:
                out.append(p)
                continue
            data = abi.call(
                abi.V3_COLLECT,
                abi.uint(p.token_id),
                abi.address(owner),
                abi.uint(abi.UINT128_MAX),
                abi.uint(abi.UINT128_MAX),
            )
            try:
                raw = await self._node.eth_call(self._d.v3_position_manager, data, sender=owner)
                owed0, owed1 = abi.words(raw)[:2]
            except Exception as exc:  # noqa: BLE001 - fees missing, position still shown
                log.warning("chain.v3_collect_failed", error=self._node.redact(str(exc)))
                owed0, owed1 = p.owed0, p.owed1
            out.append(replace(p, owed0=owed0, owed1=owed1))
        return out

    # --- v4 --------------------------------------------------------------

    async def _v4(self, owner: str, notes: list[str]) -> tuple[list[RawPosition], int]:
        manager = self._d.v4_position_manager
        count = await self._count(manager, owner, "Uniswap v4", notes)
        if not count:
            return [], 0
        candidates = await self._v4_ids(owner)
        if candidates is None:
            notes.append(f"{count} Uniswap v4 position(s) exist but could not be listed")
            return [], 0
        owners = await self._node.multicall(
            [Call(manager, abi.call(abi.OWNER_OF, abi.uint(t))) for t in candidates]
        )
        me = owner.lower()
        ids = [
            t for t, r in zip(candidates, owners, strict=True)
            if r and abi.as_address(abi.words(r)[0]) == me
        ]
        if len(ids) < count:
            notes.append(f"{count - len(ids)} Uniswap v4 position(s) could not be listed")
        return await self._v4_positions(ids)

    async def _v4_ids(self, owner: str) -> list[int] | None:
        """Candidate token IDs from the explorer, or None if it failed."""
        url = f"{self._d.blockscout}/api/v2/tokens/{self._d.v4_position_manager}/instances"
        params: dict[str, str] = {"holder_address_hash": owner}
        ids: list[int] = []
        try:
            for _ in range(EXPLORER_PAGES):
                response = await self._explorer.get(url, params=params)
                response.raise_for_status()
                body = response.json()
                ids.extend(int(item["id"]) for item in body.get("items", []))
                following = body.get("next_page_params")
                if not following:
                    break
                params = {"holder_address_hash": owner, **{k: str(v) for k, v in following.items()}}
        except Exception as exc:  # noqa: BLE001 - reported as "could not be listed"
            log.warning("chain.v4_explorer_failed", chain=self._d.chain.key, error=str(exc)[:200])
            return None
        return ids[:MAX_POSITIONS]

    async def _v4_positions(self, ids: list[int]) -> tuple[list[RawPosition], int]:
        if not ids:
            return [], 0
        manager, view = self._d.v4_position_manager, self._d.v4_state_view
        results = await self._node.multicall(
            [Call(manager, abi.call(abi.V4_POOL_AND_POSITION, abi.uint(t))) for t in ids]
            + [Call(manager, abi.call(abi.V4_POSITION_LIQUIDITY, abi.uint(t))) for t in ids]
        )
        live: list[tuple[int, list[int], int, bytes]] = []
        closed = 0
        for i, token_id in enumerate(ids):
            info, liquidity_raw = results[i], results[len(ids) + i]
            liquidity = abi.words(liquidity_raw)[0] if liquidity_raw else 0
            if not info or not liquidity:
                closed += 1
                continue
            w = abi.words(info)
            pool_id = abi.keccak256(b"".join(v.to_bytes(32, "big") for v in w[:5]))
            live.append((token_id, w, liquidity, pool_id))
        if not live:
            return [], closed
        calls: list[Call] = []
        for token_id, w, _, pool_id in live:
            lower, upper = abi.signed(w[5] >> 8, 24), abi.signed(w[5] >> 32, 24)
            pid, ticks = pool_id.hex(), abi.uint(lower) + abi.uint(upper)
            calls += [
                Call(view, abi.call(abi.V4_SLOT0, pid)),
                Call(view, abi.call(abi.V4_FEE_GROWTH_INSIDE, pid, ticks)),
                Call(view, abi.call(
                    abi.V4_POSITION_INFO, pid, abi.address(manager),
                    abi.uint(lower), abi.uint(upper), abi.uint(token_id),
                )),
            ]
        state = await self._node.multicall(calls)
        positions = [
            _v4_position(token_id, w, liquidity, state[3 * i : 3 * i + 3])
            for i, (token_id, w, liquidity, _) in enumerate(live)
        ]
        return positions, closed

    # --- shared ----------------------------------------------------------

    async def _count(self, manager: str, owner: str, name: str, notes: list[str]) -> int:
        raw = await self._node.eth_call(manager, abi.call(abi.BALANCE_OF, abi.address(owner)))
        count = abi.words(raw)[0]
        if count > MAX_POSITIONS:
            notes.append(f"only the first {MAX_POSITIONS} of {count} {name} NFTs were read")
        return min(count, MAX_POSITIONS)

    async def _present(self, raw: list[RawPosition]) -> tuple[LiquidityPosition, ...]:
        addresses = {p.token0 for p in raw} | {p.token1 for p in raw}
        tokens = await self._tokens.load(addresses)
        try:
            prices = await self._prices.usd_prices(addresses)
        except Exception as exc:  # noqa: BLE001 - shown without USD rather than not at all
            log.warning("chain.lp_prices_failed", error=self._node.redact(str(exc)))
            prices = {}
        return tuple(
            _position(p, tokens[p.token0], tokens[p.token1], prices)
            for p in sorted(raw, key=lambda p: (p.liquidity == 0, p.protocol, p.token_id))
        )


def _v4_position(
    token_id: int, key: list[int], liquidity: int, state: list[bytes | None]
) -> RawPosition:
    slot0, growth, info = state
    lower, upper = abi.signed(key[5] >> 8, 24), abi.signed(key[5] >> 32, 24)
    sqrt_price, tick = 0, 0
    if slot0:
        sqrt_price, tick = abi.words(slot0)[0], abi.signed(abi.words(slot0)[1], 24)
    owed0 = owed1 = 0
    if growth and info:
        inside0, inside1 = abi.words(growth)[:2]
        _, last0, last1 = abi.words(info)[:3]
        owed0 = fees_owed(inside0, last0, liquidity)
        owed1 = fees_owed(inside1, last1, liquidity)
    return RawPosition(
        protocol="Uniswap v4",
        token_id=token_id,
        token0=abi.as_address(key[0]),
        token1=abi.as_address(key[1]),
        fee=key[2],
        tick_lower=lower,
        tick_upper=upper,
        liquidity=liquidity,
        sqrt_price_x96=sqrt_price,
        tick=tick,
        owed0=owed0,
        owed1=owed1,
    )


def _position(
    p: RawPosition, t0: TokenInfo, t1: TokenInfo, prices: dict[str, Decimal]
) -> LiquidityPosition:
    raw0, raw1 = (
        amounts(p.liquidity, p.sqrt_price_x96, p.tick_lower, p.tick_upper)
        if p.liquidity and p.sqrt_price_x96
        else (Decimal(0), Decimal(0))
    )
    price = (
        price_from_sqrt(p.sqrt_price_x96, t0.decimals, t1.decimals)
        if p.sqrt_price_x96
        else Decimal(0)
    )
    usd0, usd1 = prices.get(t0.address), prices.get(t1.address)
    # One side priced and the pool's own price for the other. Flagged, because
    # a thin pool's price is a mark, not what the tokens would sell for.
    pool_priced = price > 0 and (usd0 is None) != (usd1 is None)
    if usd0 is None and usd1 is not None and price:
        usd0 = price * usd1
    if usd1 is None and usd0 is not None and price:
        usd1 = usd0 / price
    return LiquidityPosition(
        protocol=p.protocol,
        token_id=p.token_id,
        token0=t0,
        token1=t1,
        fee=p.fee,
        liquidity=p.liquidity,
        tick=p.tick,
        tick_lower=p.tick_lower,
        tick_upper=p.tick_upper,
        price=price,
        price_lower=price_at_tick(p.tick_lower, t0.decimals, t1.decimals),
        price_upper=price_at_tick(p.tick_upper, t0.decimals, t1.decimals),
        amount0=whole(raw0, t0.decimals),
        amount1=whole(raw1, t1.decimals),
        fees0=whole(p.owed0, t0.decimals),
        fees1=whole(p.owed1, t1.decimals),
        usd0=usd0,
        usd1=usd1,
        pool_priced=pool_priced,
    )
