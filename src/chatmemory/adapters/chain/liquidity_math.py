"""Concentrated-liquidity arithmetic, the same for Uniswap v3 and v4.

Pure functions over integers read from the chain, so every figure an answer
shows can be checked by hand. Decimal throughout: a price range reported to
four significant figures is a promise that the fifth did not move it.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

Q96 = Decimal(2**96)
Q128 = 1 << 128
UINT256 = 1 << 256
PRECISION = 60
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
RAY = Decimal(10) ** 27


def sqrt_price_at_tick(tick: int) -> Decimal:
    """sqrt(1.0001^tick), the raw (undecimalled) square-root price."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return Decimal("1.0001") ** (Decimal(tick) / 2)


def sqrt_price_from_x96(sqrt_price_x96: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return Decimal(sqrt_price_x96) / Q96


def amounts(
    liquidity: int, sqrt_price_x96: int, tick_lower: int, tick_upper: int
) -> tuple[Decimal, Decimal]:
    """Raw token0 and token1 amounts a position holds at the current price.

    The three cases of the whitepaper: below the range everything is token0,
    above it everything is token1, inside it is a mix.
    """
    with localcontext() as ctx:
        ctx.prec = PRECISION
        liq = Decimal(liquidity)
        price = sqrt_price_from_x96(sqrt_price_x96)
        lower = sqrt_price_at_tick(tick_lower)
        upper = sqrt_price_at_tick(tick_upper)
        if price <= lower:
            return liq * (upper - lower) / (lower * upper), Decimal(0)
        if price >= upper:
            return Decimal(0), liq * (upper - lower)
        return liq * (upper - price) / (price * upper), liq * (price - lower)


def price_at_tick(tick: int, decimals0: int, decimals1: int) -> Decimal:
    """Token1 per token0 in whole units at a tick."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return Decimal("1.0001") ** tick * Decimal(10) ** (decimals0 - decimals1)


def price_from_sqrt(sqrt_price_x96: int, decimals0: int, decimals1: int) -> Decimal:
    """Token1 per token0 in whole units at the pool's current price."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return sqrt_price_from_x96(sqrt_price_x96) ** 2 * Decimal(10) ** (decimals0 - decimals1)


def in_range(tick: int, tick_lower: int, tick_upper: int) -> bool:
    """Whether the position is earning. Upper bound exclusive, as in the pool."""
    return tick_lower <= tick < tick_upper


def fees_owed(fee_growth_inside: int, fee_growth_last: int, liquidity: int) -> int:
    """Raw fees accrued since the position was last touched.

    The subtraction wraps modulo 2^256 on purpose: fee growth counters are
    allowed to overflow in the contracts, and the difference is still right.
    """
    return ((fee_growth_inside - fee_growth_last) % UINT256) * liquidity // Q128


def whole(raw: Decimal | int, decimals: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return Decimal(raw) / (Decimal(10) ** decimals)


def apy_from_ray(rate_ray: int) -> Decimal:
    """Aave's per-year rate (a ray, compounded per second) as an APY fraction."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        apr = Decimal(rate_ray) / RAY
        return (1 + apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1
