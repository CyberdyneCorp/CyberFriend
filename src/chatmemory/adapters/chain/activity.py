"""What a wallet did on one chain, classified from explorer rows without a model.

Rows are grouped by transaction, and each transaction's legs -- what moved in
or out of the wallet, and with whom -- decide what it was, in this order:

1.  **Liquidity**: a Uniswap position NFT moved, or the position manager was
    called or paid. Open, add, remove or collect from the multicall's inner
    calls when the wallet sent it; from the flows otherwise.
2.  **Lending**: an Aave aToken or debt token moved. Amounts are the
    underlying's, because an aToken mint includes accrued interest.
3.  **Swap**: one asset out and a different one in.
4.  **Sent / received**: everything left, with the full counterparty.
5.  **Other**: a transaction the wallet sent that moved nothing -- an
    approval, say -- named by its method, never by a guess.

What is hidden, by rule and counted, because address poisoning is in the real
data and Blockscout called every poisoned token "ok": a token nobody listed in
a transaction the wallet did not send, a zero-value transfer, and a deposit
from a third party worth under a cent. A hidden row whose counterparty shares
its first and last four characters with a real counterparty is a lookalike,
and the answer warns about it.

A payment to a plain address inside a larger transaction -- a relayer taking
its cut of a relayed action -- is shown as its own transfer. It is not called
a fee: nothing on chain says it is one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from chatmemory.adapters.chain.aave import Wrapper, Wrapping
from chatmemory.adapters.chain.deployments import NATIVE
from chatmemory.adapters.chain.positions import TokenInfo
from chatmemory.adapters.chain.tokens import Chain

DUST_USD = Decimal("0.01")
ETHER_DECIMALS = 18
LOOKALIKE_CHARS = 4
"""How much of each end a poisoned address copies: what a wallet shows."""

Row = Mapping[str, Any]


class Kind(StrEnum):
    LIQUIDITY = "liquidity"
    LENDING = "lending"
    SWAP = "swap"
    SENT = "sent"
    RECEIVED = "received"
    OTHER = "other"


class HiddenReason(StrEnum):
    UNSOLICITED = "unsolicited"
    ZERO_VALUE = "zero_value"
    DUST = "dust"


@dataclass(frozen=True, slots=True)
class Leg:
    """One movement into (+) or out of (-) the wallet, in whole units."""

    token: str
    symbol: str
    amount: Decimal
    counterparty: str
    #: Set for an aToken or a debt token.
    wrapper: Wrapper | None = None
    #: The counterparty is a contract, or the zero address of a mint or burn.
    to_contract: bool = True
    #: The token is one this deployment recognises (named, or an Aave asset).
    known: bool = True

    @property
    def asset(self) -> str:
        """The asset behind the leg: the underlying for an Aave token."""
        return self.wrapper.underlying.address if self.wrapper else self.token


@dataclass(frozen=True, slots=True)
class Part:
    """One thing a lending transaction did: "supplied 0.02 ETH"."""

    verb: str
    amount: Decimal
    symbol: str
    asset: str


@dataclass(frozen=True, slots=True)
class Action:
    at: datetime
    kind: Kind
    legs: tuple[Leg, ...] = ()
    #: Liquidity: open, add, remove, collect, withdrawal or change.
    #: Other: the method name or selector.
    detail: str = ""
    parts: tuple[Part, ...] = ()
    #: For a transaction that moved nothing: the contract it called.
    called: str = ""
    token_id: str = ""


@dataclass(frozen=True, slots=True)
class Hidden:
    unsolicited: int = 0
    zero_value: int = 0
    dust: int = 0
    #: Of the hidden rows, those whose counterparty imitates a real one.
    lookalike: int = 0

    @property
    def total(self) -> int:
        return self.unsolicited + self.zero_value + self.dust


@dataclass(frozen=True, slots=True)
class ChainActivity:
    """One chain's answer, or why it could not be read."""

    chain: Chain
    actions: tuple[Action, ...] = ()
    gas: Decimal = Decimal(0)
    gas_transactions: int = 0
    relayed: int = 0
    hidden: Hidden = Hidden()
    truncated: bool = False
    unreachable: str | None = None
    #: USD per whole token, now, by token address (native ether included).
    prices: Mapping[str, Decimal] = field(default_factory=dict)
    #: False when Aave's tokens could not be read, so its actions may be hidden.
    wrappers_known: bool = True


@dataclass(frozen=True, slots=True)
class Recognised:
    """What this deployment knows about one chain's contracts and tokens."""

    tokens: Mapping[str, TokenInfo]
    wrappers: Mapping[str, Wrapper]
    position_managers: frozenset[str]
    weth: str
    native_symbol: str = "ETH"
    wrappers_known: bool = True


@dataclass
class Transaction:
    """Everything the rows say about one transaction, before it is judged."""

    hash: str
    at: datetime
    sent: bool = False
    fee: Decimal = Decimal(0)
    method: str = ""
    called: list[str] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    minted_nft: str = ""
    moved_nft: bool = False
    hidden: list[tuple[HiddenReason, str]] = field(default_factory=list)


# --- reading rows -----------------------------------------------------------------


def transactions(rows: Iterable[Row], wallet: str, known: Recognised) -> list[Transaction]:
    """The rows grouped by transaction, newest first, as the explorer lists them."""
    wallet = wallet.lower()
    grouped: dict[str, Transaction] = {}
    for row in rows:
        tx_hash = str(row.get("hash", "")).lower()
        tx = grouped.get(tx_hash)
        if tx is None:
            tx = grouped[tx_hash] = Transaction(tx_hash, _when(row))
        _read_row(tx, row, wallet, known)
    return list(grouped.values())


def _read_row(tx: Transaction, row: Row, wallet: str, known: Recognised) -> None:
    kind = row.get("type")
    if kind == "ERC-20":
        _token_row(tx, row, wallet, known)
    elif kind in ("ERC-721", "ERC-1155", "ERC-404"):
        _nft_row(tx, row, wallet, known)
    else:
        _native_row(tx, row, wallet, known)


def _native_row(tx: Transaction, row: Row, wallet: str, known: Recognised) -> None:
    sender, receiver = _party(row, "from"), _party(row, "to")
    if sender == wallet and row.get("internal_transaction_index") is None:
        # Only here is the fee the wallet's: on a relayed, internal or token
        # row it is somebody else's gas.
        tx.sent = True
        tx.fee += _units(row.get("fee"), ETHER_DECIMALS)
        tx.method = str(row.get("method") or "")
    if sender == wallet and receiver and receiver != wallet:
        tx.called.append(receiver)
    value = _units(row.get("value"), ETHER_DECIMALS)
    if not value or sender == receiver:
        return
    symbol = known.native_symbol
    if sender == wallet:
        tx.legs.append(Leg(NATIVE, symbol, -value, receiver, None, _contract(row, "to")))
    elif receiver == wallet:
        tx.legs.append(Leg(NATIVE, symbol, value, sender, None, _contract(row, "from")))


def _token_row(tx: Transaction, row: Row, wallet: str, known: Recognised) -> None:
    sender, receiver = _party(row, "from"), _party(row, "to")
    outgoing = sender == wallet
    counterparty = receiver if outgoing else sender
    total = row.get("total") or {}
    amount = _units(total.get("value"), int(total.get("decimals") or 0))
    if not amount:
        tx.hidden.append((HiddenReason.ZERO_VALUE, counterparty))
        return
    token = row.get("token") or {}
    address = str(token.get("address_hash", "")).lower()
    wrapper = known.wrappers.get(address)
    info = known.tokens.get(address)
    symbol = (
        wrapper.underlying.symbol if wrapper else info.symbol if info else str(token.get("symbol"))
    )
    side = "to" if outgoing else "from"
    tx.legs.append(
        Leg(
            address,
            symbol,
            -amount if outgoing else amount,
            counterparty,
            wrapper,
            _contract(row, side),
            known=wrapper is not None or info is not None,
        )
    )


def _nft_row(tx: Transaction, row: Row, wallet: str, known: Recognised) -> None:
    token = str((row.get("token") or {}).get("address_hash", "")).lower()
    sender = _party(row, "from")
    if token not in known.position_managers:
        tx.hidden.append((HiddenReason.UNSOLICITED, sender))
        return
    tx.moved_nft = True
    if sender == NATIVE and _party(row, "to") == wallet:
        tx.minted_nft = str((row.get("total") or {}).get("token_id") or "")


def _party(row: Row, side: str) -> str:
    party = row.get(side) or {}
    return str(party.get("hash", "")).lower() if isinstance(party, Mapping) else ""


def _contract(row: Row, side: str) -> bool:
    party = row.get(side) or {}
    return _party(row, side) == NATIVE or bool(party.get("is_contract"))


def _units(raw: object, decimals: int) -> Decimal:
    if raw in (None, ""):
        return Decimal(0)
    return Decimal(str(raw)) / (Decimal(10) ** decimals)


def _when(row: Row) -> datetime:
    return datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))


def assets_moved(txs: Sequence[Transaction], known: Recognised) -> set[str]:
    """The assets to price: every recognised leg's, Aave's as the underlying."""
    return {leg.asset for tx in txs for leg in tx.legs if leg.known or tx.sent}


# --- judging transactions ---------------------------------------------------------

_SELECTORS = {
    "open": frozenset({"0x88316456", "mint"}),
    "remove": frozenset({"0x0c49ccbe", "decreaseliquidity"}),
    "add": frozenset({"0x219f5d17", "increaseliquidity"}),
    "collect": frozenset({"0xfc6f7865", "collect"}),
}
"""Uniswap v3 position manager calls, by selector and by the method name the
explorer gives a verified contract. Checked in this order: a removal always
carries a collect, so collect alone means fees only."""

MULTICALL_METHODS = frozenset({"multicall", "0xac9650d8"})


def needs_selectors(txs: Sequence[Transaction], known: Recognised) -> bool:
    """Whether a position manager multicall the wallet sent is among these."""
    return any(
        tx.sent
        and tx.method.lower() in MULTICALL_METHODS
        and set(tx.called) & known.position_managers
        for tx in txs
    )


@dataclass(frozen=True, slots=True)
class Judged:
    actions: tuple[Action, ...]
    hidden: Hidden
    relayed: int
    gas: Decimal
    gas_transactions: int


def judge(
    txs: Sequence[Transaction],
    known: Recognised,
    prices: Mapping[str, Decimal],
    selectors: Mapping[str, frozenset[str]] | None = None,
) -> Judged:
    """Every transaction's actions, what was hidden and why, and the gas paid."""
    actions: list[Action] = []
    hidden: list[tuple[HiddenReason, str]] = []
    relayed = 0
    for tx in txs:
        legs, dropped = _visible(tx, prices, known)
        hidden.extend(dropped)
        found = _actions(tx, legs, known, selectors or {})
        actions.extend(found)
        relayed += bool(found) and not tx.sent and any(leg.amount < 0 for leg in legs)
    sent = [tx for tx in txs if tx.sent]
    return Judged(
        actions=tuple(actions),
        hidden=_count_hidden(hidden, actions),
        relayed=relayed,
        gas=sum((tx.fee for tx in sent), Decimal(0)),
        gas_transactions=len(sent),
    )


def _visible(
    tx: Transaction, prices: Mapping[str, Decimal], known: Recognised
) -> tuple[list[Leg], list[tuple[HiddenReason, str]]]:
    """The legs shown, and the ones hidden with their reason."""
    hidden = list(tx.hidden)
    legs: list[Leg] = []
    for leg in tx.legs:
        if leg.known or tx.sent:
            legs.append(leg)
        else:
            hidden.append((HiddenReason.UNSOLICITED, leg.counterparty))
    if _is_dust(tx, legs, prices, known):
        hidden.extend((HiddenReason.DUST, leg.counterparty) for leg in legs)
        return [], hidden
    return legs, hidden


def _is_dust(
    tx: Transaction, legs: Sequence[Leg], prices: Mapping[str, Decimal], known: Recognised
) -> bool:
    """A third party's deposit worth under a cent: the poisoner's calling card."""
    if tx.sent or tx.moved_nft or not legs or any(leg.amount < 0 for leg in legs):
        return False
    worth = [leg_usd(leg, prices, known) for leg in legs]
    return all(w is not None for w in worth) and sum(w or 0 for w in worth) < DUST_USD


def leg_usd(leg: Leg, prices: Mapping[str, Decimal], known: Recognised) -> Decimal | None:
    """The leg's worth now, or None when nothing prices it."""
    price = prices.get(leg.asset)
    if price is None and leg.asset == NATIVE:
        price = prices.get(known.weth)
    if price is None:
        info = known.tokens.get(leg.asset)
        price = Decimal(1) if info is not None and info.is_stable else None
    return None if price is None else abs(leg.amount) * price


def _actions(
    tx: Transaction,
    legs: list[Leg],
    known: Recognised,
    selectors: Mapping[str, frozenset[str]],
) -> list[Action]:
    """The transaction's main action, then any side payments as transfers."""
    side = _side_payments(legs)
    main = [leg for leg in legs if leg not in side]
    found: list[Action] = []
    if main or tx.moved_nft:
        found.append(_main_action(tx, main, known, selectors))
    elif tx.sent and not side:
        found.append(Action(tx.at, Kind.OTHER, detail=tx.method, called=_first(tx.called)))
    found.extend(Action(tx.at, Kind.SENT, (leg,)) for leg in side)
    return found


def _side_payments(legs: Sequence[Leg]) -> list[Leg]:
    """Outgoing legs to a plain address, inside a transaction doing more."""
    if len(legs) < 2:
        return []
    side = [leg for leg in legs if leg.amount < 0 and not leg.to_contract]
    return side if len(side) < len(legs) else []


def _main_action(
    tx: Transaction,
    legs: list[Leg],
    known: Recognised,
    selectors: Mapping[str, frozenset[str]],
) -> Action:
    parties = {leg.counterparty for leg in legs} | set(tx.called)
    if tx.moved_nft or parties & known.position_managers:
        detail = _liquidity_detail(tx, legs, selectors.get(tx.hash, frozenset()))
        return Action(tx.at, Kind.LIQUIDITY, tuple(legs), detail, token_id=tx.minted_nft)
    if any(leg.wrapper for leg in legs):
        return Action(tx.at, Kind.LENDING, tuple(legs), parts=lending_parts(legs, known))
    net = net_by_asset(legs)
    if any(v < 0 for v in net.values()) and any(v > 0 for v in net.values()):
        return Action(tx.at, Kind.SWAP, tuple(legs))
    kind = Kind.SENT if all(v <= 0 for v in net.values()) else Kind.RECEIVED
    return Action(tx.at, kind, tuple(legs))


def _liquidity_detail(tx: Transaction, legs: Sequence[Leg], inner: frozenset[str]) -> str:
    calls = inner | {tx.method.lower()}
    if tx.minted_nft:
        return "open"
    for detail, names in _SELECTORS.items():
        if calls & names:
            return detail
    if legs and all(leg.amount < 0 for leg in legs):
        return "add"
    if legs and all(leg.amount > 0 for leg in legs):
        # Relayed, so no input to read: liquidity, fees, or both.
        return "withdrawal"
    return "change"


def net_by_asset(legs: Iterable[Leg]) -> dict[str, Decimal]:
    net: dict[str, Decimal] = {}
    for leg in legs:
        net[leg.token] = net.get(leg.token, Decimal(0)) + leg.amount
    return {token: amount for token, amount in net.items() if amount}


# --- lending ----------------------------------------------------------------------


@dataclass
class _Reserve:
    """One reserve's nets within a transaction."""

    symbol: str
    plain: Decimal = Decimal(0)
    plain_symbol: str = ""
    supply: Decimal = Decimal(0)
    debt: Decimal = Decimal(0)


def lending_parts(legs: Sequence[Leg], known: Recognised) -> tuple[Part, ...]:
    """What an Aave transaction did, reserve by reserve, in underlying units."""
    reserves: dict[str, _Reserve] = {}
    for leg in legs:
        key = known.weth if leg.token == NATIVE else leg.asset
        reserve = reserves.setdefault(key, _Reserve(leg.symbol))
        _add_leg(reserve, leg)
    parts: list[Part] = []
    for key, reserve in reserves.items():
        parts.extend(_reserve_parts(key, reserve))
    return tuple(parts)


def _add_leg(reserve: _Reserve, leg: Leg) -> None:
    if leg.wrapper is None:
        reserve.plain += leg.amount
        reserve.plain_symbol = leg.symbol
    elif leg.wrapper.kind is Wrapping.SUPPLY:
        reserve.supply += leg.amount
    else:
        reserve.debt += leg.amount


def _reserve_parts(key: str, r: _Reserve) -> list[Part]:
    parts: list[Part] = []
    plain_symbol = r.plain_symbol or r.symbol
    if r.debt:
        verb = "borrowed" if r.debt > 0 else "repaid"
        parts.append(Part(verb, abs(r.debt), r.symbol, key))
        # The borrowed asset arriving, or the repaid one leaving, is the same
        # money as the debt change and is not said twice.
        if r.plain and (r.debt > 0) == (r.plain > 0):
            r.plain = r.plain - r.debt if abs(r.plain) > abs(r.debt) else Decimal(0)
    if r.supply:
        parts.append(_supply_part(key, r, plain_symbol))
    if r.plain:
        verb = "received" if r.plain > 0 else "paid"
        parts.append(Part(verb, abs(r.plain), plain_symbol, key))
    return parts


def _supply_part(key: str, r: _Reserve, plain_symbol: str) -> Part:
    """A supply or withdrawal, in the underlying that moved when it did.

    The underlying is the true figure -- the aToken mint carries interest --
    but only when it is most of the aToken change; a crumb of leftover
    underlying beside a large aToken move is reported on its own line.
    """
    supplying = r.supply > 0
    verb = "supplied" if supplying else "withdrew"
    matching = (r.plain < 0) if supplying else (r.plain > 0)
    if matching and abs(r.plain) * 2 >= abs(r.supply):
        amount, r.plain = abs(r.plain), Decimal(0)
        return Part(verb, amount, plain_symbol, key)
    return Part(verb, abs(r.supply), r.symbol, key)


# --- hidden rows ------------------------------------------------------------------


def _count_hidden(hidden: Sequence[tuple[HiddenReason, str]], actions: Sequence[Action]) -> Hidden:
    real = {
        leg.counterparty
        for action in actions
        for leg in action.legs
        if leg.counterparty and leg.counterparty != NATIVE
    }
    counts = {reason: 0 for reason in HiddenReason}
    lookalike = 0
    for reason, counterparty in hidden:
        counts[reason] += 1
        lookalike += any(looks_like(counterparty, r) for r in real)
    return Hidden(
        unsolicited=counts[HiddenReason.UNSOLICITED],
        zero_value=counts[HiddenReason.ZERO_VALUE],
        dust=counts[HiddenReason.DUST],
        lookalike=lookalike,
    )


def looks_like(candidate: str, real: str) -> bool:
    """A different address with the same first and last four characters."""
    a, b = candidate.lower(), real.lower()
    if a == b or len(a) != len(b) or len(a) < 2 + 2 * LOOKALIKE_CHARS:
        return False
    head = slice(2, 2 + LOOKALIKE_CHARS)
    return a[head] == b[head] and a[-LOOKALIKE_CHARS:] == b[-LOOKALIKE_CHARS:]


def _first(items: Sequence[str]) -> str:
    return items[0] if items else ""
