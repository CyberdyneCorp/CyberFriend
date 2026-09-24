"""What a portfolio lookup found, and the arithmetic that turns it into a total.

Three sections per chain -- what the wallet holds, its Uniswap positions, its
Aave account -- each read separately and each able to fail on its own. The
total is computed here, from the sections that were read, and says which were
not: a chain that could not be read must never quietly lower a total, because
"$1,800" and "at least $1,800" are different answers and only one of them is
true.

Nothing is counted twice by construction rather than by filtering:

*   the wallet section reads underlying tokens only (the named set and each
    chain's Aave reserve assets), never an aToken or a debt token, so an Aave
    supply is counted once, in the Aave section;
*   a Uniswap position is an NFT, and its tokens sit in the pool, not in the
    wallet;
*   native ETH and WETH are separate balances, and adding both is correct.

The Aave net is per asset -- (supplied - borrowed) x oracle price -- not the
account's collateral minus debt, which leaves out any supply not enabled as
collateral.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from chatmemory.adapters.chain.positions import ChainLending, ChainLiquidity
from chatmemory.adapters.chain.tokens import Chain

ZERO = Decimal(0)


class Section(StrEnum):
    WALLET = "wallet"
    LIQUIDITY = "liquidity"
    LENDING = "lending"


@dataclass(frozen=True, slots=True)
class Held:
    """One balance in the wallet itself, in whole units."""

    symbol: str
    amount: Decimal
    #: USD per whole token, or None when nothing prices it. Unpriced is named
    #: in the answer and left out of the sum, never counted as zero.
    usd_price: Decimal | None

    @property
    def usd(self) -> Decimal | None:
        return None if self.usd_price is None else self.amount * self.usd_price


@dataclass(frozen=True, slots=True)
class ChainWallet:
    chain: Chain
    holdings: tuple[Held, ...] = ()
    #: Ether was priced by CoinGecko because this chain's oracle did not answer.
    fallback_priced: bool = False
    unreachable: str = ""


@dataclass(frozen=True, slots=True)
class ChainPortfolio:
    """One address on one chain: three sections, each read or not."""

    wallet: ChainWallet
    liquidity: ChainLiquidity
    lending: ChainLending

    @property
    def chain(self) -> Chain:
        return self.wallet.chain

    def unreadable(self) -> tuple[Section, ...]:
        found = (
            (Section.WALLET, self.wallet.unreachable),
            (Section.LIQUIDITY, self.liquidity.unreachable),
            (Section.LENDING, self.lending.unreachable),
        )
        return tuple(section for section, reason in found if reason)

    def wallet_usd(self) -> Decimal:
        if self.wallet.unreachable:
            return ZERO
        return sum((h.usd for h in self.wallet.holdings if h.usd is not None), ZERO)

    def fees_usd(self) -> Decimal:
        """Uncollected fees of the positions whose value is known."""
        if self.liquidity.unreachable:
            return ZERO
        return sum(
            (p.fees_usd() or ZERO for p in self.liquidity.positions if p.value_usd() is not None),
            ZERO,
        )

    def liquidity_usd(self) -> Decimal:
        """What the positions hold, uncollected fees included: they are the
        owner's to collect, and leaving them out understates the position."""
        if self.liquidity.unreachable:
            return ZERO
        held = sum(
            (v for p in self.liquidity.positions if (v := p.value_usd()) is not None), ZERO
        )
        return held + self.fees_usd()

    def lending_net_usd(self) -> Decimal:
        if self.lending.unreachable:
            return ZERO
        return sum(
            (
                (a.supplied - a.borrowed) * a.usd_price
                for a in self.lending.assets
                if a.usd_price is not None
            ),
            ZERO,
        )

    def usd(self) -> Decimal:
        return self.wallet_usd() + self.liquidity_usd() + self.lending_net_usd()

    def unpriced(self) -> tuple[str, ...]:
        """What is held here and has no price, so is not in the sum."""
        wallet = [h.symbol for h in self.wallet.holdings if h.usd_price is None and h.amount]
        pools = [
            f"{p.protocol} #{p.token_id}"
            for p in self.liquidity.positions
            if p.value_usd() is None
        ]
        aave = [
            f"Aave {a.token.symbol}"
            for a in self.lending.assets
            if a.usd_price is None and (a.supplied or a.borrowed)
        ]
        return (*wallet, *pools, *aave)

    @property
    def empty(self) -> bool:
        """Read in full and holding nothing at all."""
        return (
            not self.unreadable()
            and not self.wallet.holdings
            and not self.liquidity.positions
            and not self.liquidity.notes
            and self.lending.empty
        )


@dataclass(frozen=True, slots=True)
class WalletPortfolio:
    address: str
    chains: tuple[ChainPortfolio, ...]


@dataclass(frozen=True, slots=True)
class Total:
    usd: Decimal
    #: (chain, section) pairs that could not be read. Any at all makes the
    #: figure a lower bound, and the answer says "at least".
    missing: tuple[tuple[Chain, Section], ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing


def total(wallets: Sequence[WalletPortfolio]) -> Total:
    """The sum over every wallet and chain, and what it could not include."""
    usd = ZERO
    missing: list[tuple[Chain, Section]] = []
    for wallet in wallets:
        for chain in wallet.chains:
            usd += chain.usd()
            missing.extend(
                (chain.chain, s) for s in chain.unreadable() if (chain.chain, s) not in missing
            )
    return Total(usd=usd, missing=tuple(missing))
