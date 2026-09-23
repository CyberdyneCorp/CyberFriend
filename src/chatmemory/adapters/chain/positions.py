"""What a positions lookup found, per chain, and the token facts it needs.

Every result type carries `unreachable`, as `ChainBalances` does: a chain that
could not be read is a different answer from a chain where the address has no
positions, and the two must never render the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.deployments import NATIVE
from chatmemory.adapters.chain.node import Call, Node
from chatmemory.adapters.chain.tokens import Chain

STABLECOINS = frozenset({
    "USDC", "USDT", "DAI", "USDC.E", "USDBC", "USDS", "GHO", "FRAX", "LUSD",
    "PYUSD", "USDT0", "USDE", "CRVUSD",
})
ETHER = frozenset({"ETH", "WETH"})


@dataclass(frozen=True, slots=True)
class TokenInfo:
    address: str
    symbol: str
    decimals: int

    @property
    def is_stable(self) -> bool:
        return self.symbol.upper() in STABLECOINS

    @property
    def is_ether(self) -> bool:
        return self.symbol.upper() in ETHER


@dataclass(frozen=True, slots=True)
class LiquidityPosition:
    """One Uniswap position, in whole units, priced where a price is known."""

    protocol: str
    token_id: int
    token0: TokenInfo
    token1: TokenInfo
    #: Fee tier in hundredths of a basis point (500 = 0.05%).
    fee: int
    liquidity: int
    tick: int
    tick_lower: int
    tick_upper: int
    #: Token1 per token0, whole units.
    price: Decimal
    price_lower: Decimal
    price_upper: Decimal
    amount0: Decimal
    amount1: Decimal
    fees0: Decimal
    fees1: Decimal
    usd0: Decimal | None = None
    usd1: Decimal | None = None
    #: One side has no oracle price and was valued at this pool's own price,
    #: which for a thin pool can be far from anything it would sell for.
    pool_priced: bool = False

    @property
    def in_range(self) -> bool:
        return self.liquidity > 0 and self.tick_lower <= self.tick < self.tick_upper

    def value_usd(self) -> Decimal | None:
        return _usd(self.amount0, self.usd0, self.amount1, self.usd1)

    def fees_usd(self) -> Decimal | None:
        return _usd(self.fees0, self.usd0, self.fees1, self.usd1)


def _usd(a0: Decimal, p0: Decimal | None, a1: Decimal, p1: Decimal | None) -> Decimal | None:
    # A side holding nothing needs no price; a side holding something does.
    if (a0 and p0 is None) or (a1 and p1 is None):
        return None
    return a0 * (p0 or 0) + a1 * (p1 or 0)


@dataclass(frozen=True, slots=True)
class ChainLiquidity:
    chain: Chain
    #: Open positions only; withdrawn NFTs are neither listed nor counted.
    positions: tuple[LiquidityPosition, ...] = ()
    #: Things that could not be read, said in the answer rather than hidden.
    notes: tuple[str, ...] = ()
    unreachable: str = ""


@dataclass(frozen=True, slots=True)
class LendingAsset:
    token: TokenInfo
    supplied: Decimal
    borrowed: Decimal
    supply_apy: Decimal
    borrow_apy: Decimal
    usd_price: Decimal | None
    collateral: bool


@dataclass(frozen=True, slots=True)
class ChainLending:
    chain: Chain
    collateral_usd: Decimal = Decimal(0)
    debt_usd: Decimal = Decimal(0)
    available_usd: Decimal = Decimal(0)
    #: None when there is no debt: the contract reports uint256 max, which is
    #: "not applicable", not a number anybody should read.
    health_factor: Decimal | None = None
    assets: tuple[LendingAsset, ...] = ()
    unreachable: str = ""

    @property
    def empty(self) -> bool:
        return not self.assets and not self.collateral_usd and not self.debt_usd


@dataclass
class TokenDirectory:
    """Symbols and decimals, read once per lookup and shared by its readers."""

    node: Node
    _known: dict[str, TokenInfo] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._known[NATIVE] = TokenInfo(NATIVE, "ETH", 18)

    async def load(self, addresses: set[str]) -> dict[str, TokenInfo]:
        wanted = sorted(a.lower() for a in addresses if a.lower() not in self._known)
        if wanted:
            calls = [Call(a, abi.SYMBOL) for a in wanted] + [Call(a, abi.DECIMALS) for a in wanted]
            results = await self.node.multicall(calls)
            for i, token in enumerate(wanted):
                symbol_raw, decimals_raw = results[i], results[len(wanted) + i]
                symbol = abi.decode_string(symbol_raw) if symbol_raw else ""
                decimals = abi.words(decimals_raw)[0] if decimals_raw else 18
                self._known[token] = TokenInfo(token, symbol or token[:8], int(decimals))
        return {a.lower(): self._known[a.lower()] for a in addresses}
