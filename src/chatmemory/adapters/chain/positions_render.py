"""Positions as Discord markdown, figures only.

Shown verbatim (see `verbatim_answer`), so the first line is a header the
citation already carries and is dropped, and everything after it is exactly
what the person reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from chatmemory.adapters.chain.positions import (
    ChainLending,
    ChainLiquidity,
    LendingAsset,
    LiquidityPosition,
    TokenInfo,
)

FULL_RANGE_TICK = 887_000
"""Beyond this either side, a range is the whole curve (the limit is 887272)."""

DYNAMIC_FEE = 0x800000
"""v4's flag for a pool whose hook sets the fee per swap."""

LIQUIDATION_WATCH = Decimal("1.1")
DUST_USD = Decimal("0.01")


def amount(value: Decimal) -> str:
    """Readable at any magnitude: cents for big figures, significance for dust."""
    magnitude = abs(value)
    if magnitude == 0:
        return "0"
    if magnitude >= 1000:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if magnitude >= Decimal("0.0001"):
        return f"{value:.6g}"
    if magnitude >= Decimal("1e-10"):
        # Fixed point, not "8.805e-5": people read fees in ETH as decimals.
        return f"{value:.12f}".rstrip("0")
    return f"{value:.3e}"


def usd(value: Decimal | None) -> str:
    return "" if value is None else f" ≈ ${value:,.2f}"


def fee_tier(fee: int) -> str:
    if fee & DYNAMIC_FEE:
        return "dynamic fee"
    return f"{Decimal(fee) / 10000:f}".rstrip("0").rstrip(".") + "%"


def _quote(p: LiquidityPosition) -> tuple[TokenInfo, TokenInfo, Decimal, Decimal, Decimal]:
    """(base, quote, price, low, high) in the orientation people read.

    Priced in the stablecoin when there is one, else in ether, else as the
    pool orders them. The pool's own price is token1 per token0.
    """
    t0, t1 = p.token0, p.token1
    invert = (t0.is_stable and not t1.is_stable) or (
        t0.is_ether and not t1.is_ether and not t1.is_stable
    )
    if not invert or not p.price:
        return t0, t1, p.price, p.price_lower, p.price_upper
    return t1, t0, 1 / p.price, 1 / p.price_upper, 1 / p.price_lower


def _range_line(p: LiquidityPosition) -> str:
    base, quote, price, low, high = _quote(p)
    now = f" · now {amount(price)}" if price else ""
    if p.tick_lower <= -FULL_RANGE_TICK and p.tick_upper >= FULL_RANGE_TICK:
        return f"  Range: full range{now} {quote.symbol} per {base.symbol}"
    return f"  Range: {amount(low)} – {amount(high)} {quote.symbol} per {base.symbol}{now}"


def _status(p: LiquidityPosition) -> str:
    return "🟢 in range" if p.in_range else "🔴 out of range"


def _pair(a0: Decimal, a1: Decimal, p: LiquidityPosition) -> str:
    parts = [f"{amount(a)} {t.symbol}" for a, t in ((a0, p.token0), (a1, p.token1)) if a]
    return " + ".join(parts) or "nothing"


def position_lines(p: LiquidityPosition) -> list[str]:
    head = (
        f"• {p.protocol} #{p.token_id} · {p.token0.symbol}/{p.token1.symbol} "
        f"{fee_tier(p.fee)} · {_status(p)}"
    )
    lines = [head, _range_line(p)]
    lines.append(f"  Holds: {_pair(p.amount0, p.amount1, p)}{usd(p.value_usd())}")
    lines.append(f"  Uncollected: {_pair(p.fees0, p.fees1, p)}{usd(p.fees_usd())}")
    if p.pool_priced:
        lines.append("  (one token has no oracle price; valued at this pool's own price)")
    return lines


def render_liquidity(address: str, chains: Sequence[ChainLiquidity]) -> str:
    lines = [f"Liquidity positions for `{address}`"]
    for chain in chains:
        lines.append("")
        if chain.unreachable:
            lines.append(f"**{chain.chain.name}** — could not be read ({chain.unreachable})")
            continue
        if not chain.positions:
            lines.append(f"**{chain.chain.name}** — no open Uniswap positions")
        else:
            lines.append(f"**{chain.chain.name}**")
            for position in chain.positions:
                lines.extend(position_lines(position))
        lines.extend(f"_{note}._" for note in chain.notes)
    lines.append("")
    lines.append("_Open Uniswap v3 and v4 positions only; other exchanges are not read._")
    return "\n".join(lines)


def _asset_line(asset: LendingAsset, held: Decimal, rate: Decimal, *, supply: bool) -> str:
    value = usd(held * asset.usd_price) if asset.usd_price is not None else ""
    tail = " · collateral" if supply and asset.collateral else ""
    return f"  • {amount(held)} {asset.token.symbol}{value} · {rate:.2%} APY{tail}"


def _dust(asset: LendingAsset, held: Decimal) -> bool:
    """Worth less than a cent. Only when priced: unpriced is not the same as tiny."""
    return bool(held) and asset.usd_price is not None and held * asset.usd_price < DUST_USD


def _health(chain: ChainLending) -> str:
    if chain.health_factor is None:
        return "Health factor: n/a (no debt)"
    text = f"Health factor: {chain.health_factor:.2f}"
    if chain.health_factor < LIQUIDATION_WATCH:
        text += " ⚠️ (liquidation happens below 1.00)"
    return text


def render_lending(address: str, chains: Sequence[ChainLending]) -> str:
    lines = [f"Aave positions for `{address}`"]
    for chain in chains:
        lines.append("")
        if chain.unreachable:
            lines.append(f"**{chain.chain.name}** — could not be read ({chain.unreachable})")
            continue
        if chain.empty:
            lines.append(f"**{chain.chain.name}** — no Aave position")
            continue
        lines.append(f"**{chain.chain.name}** · Aave v3")
        lines.append(
            f"Collateral ${chain.collateral_usd:,.2f} · Debt ${chain.debt_usd:,.2f} · "
            f"{_health(chain)}"
        )
        supplied = [a for a in chain.assets if a.supplied and not _dust(a, a.supplied)]
        borrowed = [a for a in chain.assets if a.borrowed and not _dust(a, a.borrowed)]
        dust = sum(1 for a in chain.assets if _dust(a, a.supplied) or _dust(a, a.borrowed))
        if supplied:
            lines.append("Supplied:")
            lines.extend(_asset_line(a, a.supplied, a.supply_apy, supply=True) for a in supplied)
        if borrowed:
            lines.append("Borrowed:")
            lines.extend(_asset_line(a, a.borrowed, a.borrow_apy, supply=False) for a in borrowed)
        if dust:
            lines.append(f"_{dust} balance(s) under $0.01 not shown._")
    lines.append("")
    lines.append("_Aave v3 main market on each chain; other markets are not read._")
    return "\n".join(lines)
