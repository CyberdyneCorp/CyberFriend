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

LOG_WINDOW = 10_000
"""Blocks per `eth_getLogs`: Infura's limit."""

RECENT_LOG_WINDOWS = 24
"""How far back to look for v4 positions the explorer has not indexed yet.

The explorer lags: a position minted twenty minutes before the question was
missing from Blockscout on Arbitrum. 24 windows is ~17 hours on Arbitrum,
~5.5 days on Base, ~33 days on Ethereum -- and the scan stops as soon as it
has found as many as the chain says exist."""


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
    #: The v3 pool address or the v4 pool id, which is what an alert pins.
    pool_ref: str = ""


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
        raw = await self._v3(owner, notes) + await self._v4(owner, notes)
        positions = await self._present(raw) if raw else ()
        return ChainLiquidity(chain=self._d.chain, positions=positions, notes=tuple(notes))

    # --- v3 --------------------------------------------------------------

    async def _v3(self, owner: str, notes: list[str]) -> list[RawPosition]:
        manager = self._d.v3_position_manager
        count = await self._count(manager, owner, "Uniswap v3", notes)
        if not count:
            return []
        indexes = await self._node.multicall([
            Call(manager, abi.call(abi.TOKEN_OF_OWNER_BY_INDEX, abi.address(owner), abi.uint(i)))
            for i in range(count)
        ])
        ids = [abi.words(r)[0] for r in indexes if r]
        details = await self._node.multicall(
            [Call(manager, abi.call(abi.V3_POSITIONS, abi.uint(t))) for t in ids]
        )
        positions: list[RawPosition] = []
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
            # Open positions only. A wallet that has LP'd for a while holds
            # dozens of withdrawn NFTs, and listing or counting them buried
            # the ones that are earning.
            if position.liquidity:
                positions.append(position)
        positions = await self._v3_pool_state(positions)
        return await self._v3_fees(owner, positions)

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
            key = (p.token0, p.token1, p.fee)
            sqrt_price, tick = state.get(key, (0, 0))
            out.append(
                replace(p, sqrt_price_x96=sqrt_price, tick=tick, pool_ref=pool_of.get(key, ""))
            )
        return out

    async def _v3_fees(self, owner: str, positions: list[RawPosition]) -> list[RawPosition]:
        """Uncollected fees, by simulating `collect` as the owner.

        One `eth_call` each: the manager only lets the owner collect, and a
        multicall's sender is the multicall contract.
        """
        out: list[RawPosition] = []
        for p in positions:
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

    async def _v4(self, owner: str, notes: list[str]) -> list[RawPosition]:
        manager = self._d.v4_position_manager
        count = await self._count(manager, owner, "Uniswap v4", notes)
        if not count:
            return []
        ids = await self._owned(manager, owner, await self._v4_ids(owner) or [])
        if len(ids) < count:
            # The explorer is behind, down, or both: look for recent mints and
            # transfers in directly, and still confirm each on-chain.
            recent = await self._recent_transfers_in(owner, set(ids), count - len(ids))
            ids += await self._owned(manager, owner, recent)
        if len(ids) < count:
            notes.append(f"{count - len(ids)} Uniswap v4 position(s) could not be listed")
        return await self._v4_positions(ids)

    async def _owned(self, manager: str, owner: str, candidates: list[int]) -> list[int]:
        """The candidates the chain says `owner` holds now. Nothing else counts."""
        if not candidates:
            return []
        owners = await self._node.multicall(
            [Call(manager, abi.call(abi.OWNER_OF, abi.uint(t))) for t in candidates]
        )
        me = owner.lower()
        return [
            t for t, r in zip(candidates, owners, strict=True)
            if r and abi.as_address(abi.words(r)[0]) == me
        ]

    async def _recent_transfers_in(self, owner: str, known: set[int], missing: int) -> list[int]:
        """Token IDs transferred to `owner` in recent blocks, newest first."""
        topics: list[str | None] = [abi.TRANSFER_TOPIC, None, "0x" + abi.address(owner)]
        found: list[int] = []
        high = await self._node.block_number()
        for _ in range(RECENT_LOG_WINDOWS):
            low = max(0, high - LOG_WINDOW + 1)
            for entry in await self._node.logs(self._d.v4_position_manager, topics, low, high):
                token = _token_id(entry)
                if token is not None and token not in known and token not in found:
                    found.append(token)
            if len(found) >= missing or low == 0:
                break
            high = low - 1
        return found

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
        except Exception as exc:  # noqa: BLE001 - recent blocks are scanned instead
            log.warning("chain.v4_explorer_failed", chain=self._d.chain.key, error=str(exc)[:200])
            return None
        return ids[:MAX_POSITIONS]

    async def _v4_positions(self, ids: list[int]) -> list[RawPosition]:
        """Open positions only: an ID with no liquidity is dropped."""
        if not ids:
            return []
        manager, view = self._d.v4_position_manager, self._d.v4_state_view
        results = await self._node.multicall(
            [Call(manager, abi.call(abi.V4_POOL_AND_POSITION, abi.uint(t))) for t in ids]
            + [Call(manager, abi.call(abi.V4_POSITION_LIQUIDITY, abi.uint(t))) for t in ids]
        )
        live: list[tuple[int, list[int], int, bytes]] = []
        for i, token_id in enumerate(ids):
            info, liquidity_raw = results[i], results[len(ids) + i]
            liquidity = abi.words(liquidity_raw)[0] if liquidity_raw else 0
            if not info or not liquidity:
                continue
            w = abi.words(info)
            pool_id = abi.keccak256(b"".join(v.to_bytes(32, "big") for v in w[:5]))
            live.append((token_id, w, liquidity, pool_id))
        if not live:
            return []
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
            replace(
                _v4_position(token_id, w, liquidity, state[3 * i : 3 * i + 3]),
                pool_ref="0x" + pool_id.hex(),
            )
            for i, (token_id, w, liquidity, pool_id) in enumerate(live)
        ]
        return positions

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
            for p in sorted(raw, key=lambda p: (p.protocol, p.token_id))
        )


def _token_id(entry: dict[str, object]) -> int | None:
    topics = entry.get("topics")
    if not isinstance(topics, list) or len(topics) < 4:
        return None
    return int(str(topics[3]), 16)


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
        pool_ref=p.pool_ref,
    )
