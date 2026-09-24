"""Which chain lookup a question is, as one label, in one order of precedence.

The chain questions were three predicates checked in sequence inside the
answer service, each with its own branch. Here they are one decision: the
predicates run in a fixed precedence and return a label, and the service
answers each label from one table. A new chain route is a label, a predicate
and a table row -- never another branch.

The labels are spelled as the planned single router spells them, so that
router can take this precedence and these predicates over unchanged.

Precedence, and why:

*   WALLET_ACTIVITY first. "What did my wallet do this week" names a wallet,
    and "minhas transações de ontem" names nothing else: history was asked
    for, and a balance route would answer a different question.
*   PORTFOLIO next. "What's my wallet's total balance" names a wallet and a
    balance, and it is everything that was asked about.
*   The positions labels next. "My pools on 0x..." names an address too, and
    it is positions that were asked about.
*   WALLET_BALANCE last: an address alone, or a wallet with a balance word.

A label is decided from the words alone. Whether there is an address to read
-- typed, carried from the asker's earlier question, or saved -- is the
service's to settle, because only it holds the asker's saved values; with
none, the answer is a request for one (decision `wallet_address_missing`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from chatmemory.app.routing import (
    PositionKind,
    defi_question,
    portfolio_question,
    wallet_question,
)
from chatmemory.app.wallet_activity import activity_question


class CryptoRoute(StrEnum):
    WALLET_ACTIVITY = "WALLET_ACTIVITY"
    PORTFOLIO = "PORTFOLIO"
    DEFI_LIQUIDITY = "DEFI_LIQUIDITY"
    DEFI_LENDING = "DEFI_LENDING"
    DEFI_BOTH = "DEFI_BOTH"
    WALLET_BALANCE = "WALLET_BALANCE"


DEFI_ROUTES = {
    PositionKind.LIQUIDITY: CryptoRoute.DEFI_LIQUIDITY,
    PositionKind.LENDING: CryptoRoute.DEFI_LENDING,
    PositionKind.BOTH: CryptoRoute.DEFI_BOTH,
}


@dataclass(frozen=True, slots=True)
class CryptoQuery:
    """A chain question: what to read, and whose addresses.

    `addresses` are the ones written in the question, or carried from the
    asker's own earlier one (`carried`). `mine` says the asker's saved wallet
    belongs in the lookup too: always when nothing was written, and for a
    portfolio also alongside what was.
    """

    route: CryptoRoute
    addresses: tuple[str, ...] = ()
    mine: bool = False
    carried: bool = False


def crypto_route(text: str, previous: Sequence[str] = ()) -> CryptoQuery | None:
    """The chain lookup this question is, or None when it is not one.

    `previous` is the asker's own earlier questions, oldest first, for a
    follow-up that names no address ("and in total?", "show me the v4 one").
    """
    activity = activity_question(text, previous)
    if activity is not None:
        return CryptoQuery(
            CryptoRoute.WALLET_ACTIVITY,
            () if activity.address is None else (activity.address,),
            mine=activity.address is None,
            carried=activity.carried,
        )
    portfolio = portfolio_question(text, previous)
    if portfolio is not None:
        return CryptoQuery(
            CryptoRoute.PORTFOLIO,
            portfolio.addresses,
            mine=portfolio.mine,
            carried=portfolio.carried,
        )
    defi = defi_question(text, previous)
    if defi is not None:
        return CryptoQuery(
            DEFI_ROUTES[defi.kind],
            () if defi.address is None else (defi.address,),
            mine=defi.address is None,
            carried=defi.carried,
        )
    wallet = wallet_question(text)
    if wallet is not None:
        return CryptoQuery(
            CryptoRoute.WALLET_BALANCE,
            () if wallet.address is None else (wallet.address,),
            mine=wallet.address is None,
        )
    return None
