"""Position alerts: a stored condition on somebody's position, and its last state.

An alert is not a question replayed on a timer -- that is a scheduled task. It
is a condition read straight from the chain (is this Uniswap position in range,
is this Aave health factor above a limit) plus what the last reading said, so
the sweep can tell a *change* from a repeat and message only on the change.

Everything a sweep needs is stored when the alert is created: the chain, the
position's identity and pool, the token symbols and decimals, the language the
message is written in. So a sweep never discovers anything and never runs a
model; it reads a handful of words per alert and compares them.

The address is stored already cleared. The per-call egress guard roots an
address in the asker's own words; a sweep has no asker and no words, so the
check is made once, at creation, and the stored address is the result of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from chatmemory.domain.identity import PersonRef

MIN_THRESHOLD = Decimal("1.05")
MAX_THRESHOLD = Decimal("5.0")
"""Health-factor limits a person may set. Below 1.05 the message would arrive
about when the liquidation does; above 5 it is not a warning of anything."""

REARM_MARGIN = Decimal("0.05")
"""How far above the limit a health factor must climb before a fired alert
re-arms. Without it a factor hovering on the limit messages every sweep."""

LP_CONFIRMATIONS = 2
"""Consecutive agreeing reads before a range change counts. One block's wick
across a range edge is not worth a message; two sweeps apart is."""

HEALTH_CONFIRMATIONS = 1
"""Aave fires on the first read below the limit: near liquidation, five
minutes is the difference that matters."""

DEFAULT_ALERTS_PER_PERSON = 10
"""Active alerts one person may keep, separate from their scheduled tasks."""

POSITION_CLOSED = "position closed"
DIRECT_MESSAGES_CLOSED = "direct messages are closed"

CHAIN_NAMES: Mapping[str, str] = {
    "ethereum": "Ethereum",
    "base": "Base",
    "arbitrum": "Arbitrum",
}
"""The chains an alert may watch, by stored key, with the name a message uses."""


class AlertKind(StrEnum):
    LP_RANGE = "lp_range"
    AAVE_HEALTH = "aave_health"


class AlertState(StrEnum):
    """What the last reading said. UNKNOWN until the first one."""

    UNKNOWN = "unknown"
    IN_RANGE = "in_range"
    OUT_OF_RANGE = "out_of_range"
    CLOSED = "closed"
    OK = "ok"
    BELOW = "below"
    NO_DEBT = "no_debt"


class LpProtocol(StrEnum):
    UNISWAP_V3 = "uniswap_v3"
    UNISWAP_V4 = "uniswap_v4"

    @property
    def label(self) -> str:
        return "Uniswap v3" if self is LpProtocol.UNISWAP_V3 else "Uniswap v4"


class AlertLanguage(StrEnum):
    ENGLISH = "en"
    PORTUGUESE = "pt"


class AddressSource(StrEnum):
    """Where the watched address came from. A saved one is forgotten with the fact."""

    SAVED = "saved"
    TYPED = "typed"


class AlertRefusal(StrEnum):
    """Why an alert was not created. Each maps to a sentence the person is shown."""

    THRESHOLD_OUT_OF_RANGE = "threshold_out_of_range"
    INVALID_TARGET = "invalid_target"
    AT_CAP = "at_cap"
    DUPLICATE = "duplicate"
    UNKNOWN_PERSON = "unknown_person"


@dataclass(frozen=True, slots=True)
class LpTarget:
    """One Uniswap position, pinned when the alert was made.

    `pool_ref` is the v3 pool address, or the v4 pool id: what the sweep reads
    the current tick from without discovering anything.
    """

    protocol: LpProtocol
    token_id: int
    pool_ref: str
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    #: Fee tier in hundredths of a basis point (500 = 0.05%).
    fee: int


@dataclass(frozen=True, slots=True)
class NewAlert:
    """An alert as creation hands it over: target, owner and baseline."""

    person: PersonRef
    kind: AlertKind
    chain: str
    #: Already cleared: see the module docstring.
    address: str
    address_source: AddressSource
    language: AlertLanguage
    lp: LpTarget | None = None
    threshold: Decimal | None = None
    #: "Back in range" and "recovered" messages. On unless turned off.
    notify_return: bool = True
    #: The reading taken at creation; nothing is sent until it changes.
    state: AlertState = AlertState.UNKNOWN
    last_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PositionAlert:
    """One stored alert, with its owner and what the last reading said."""

    id: int
    person: PersonRef
    kind: AlertKind
    chain: str
    address: str
    language: AlertLanguage
    state: AlertState
    state_since: datetime
    created_at: datetime
    next_check_at: datetime
    address_source: AddressSource = AddressSource.TYPED
    lp: LpTarget | None = None
    threshold: Decimal | None = None
    notify_return: bool = True
    pending_state: AlertState | None = None
    pending_count: int = 0
    last_value: Decimal | None = None
    consecutive_failures: int = 0
    last_checked_at: datetime | None = None
    last_fired_at: datetime | None = None
    disabled_at: datetime | None = None
    disabled_reason: str = ""

    @property
    def active(self) -> bool:
        return self.disabled_at is None

    @property
    def chain_name(self) -> str:
        return CHAIN_NAMES.get(self.chain, self.chain)


# --- what a sweep reads ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class LpObservation:
    """One pinned position as the chain describes it now.

    `owned` is False when the NFT is burned or no longer held by the watched
    address; with no liquidity either way the position is closed. Prices are
    already oriented the way people read them (quote per base).
    """

    owned: bool
    liquidity: int = 0
    tick: int = 0
    tick_lower: int = 0
    tick_upper: int = 0
    price: Decimal = Decimal(0)
    price_lower: Decimal = Decimal(0)
    price_upper: Decimal = Decimal(0)
    base_symbol: str = ""
    quote_symbol: str = ""

    @property
    def state(self) -> AlertState:
        if not self.owned or not self.liquidity:
            return AlertState.CLOSED
        if self.tick_lower <= self.tick < self.tick_upper:
            return AlertState.IN_RANGE
        return AlertState.OUT_OF_RANGE


@dataclass(frozen=True, slots=True)
class HealthObservation:
    """An Aave account now. `health_factor` is None when there is no debt."""

    health_factor: Decimal | None
    collateral_usd: Decimal = Decimal(0)
    debt_usd: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class ReadFailure:
    """The reading could not be made. Never a state, never a message."""

    reason: str


Observation = LpObservation | HealthObservation | ReadFailure


@dataclass(frozen=True, slots=True)
class AlertUpdate:
    """What one sweep decided about one alert, as the store records it."""

    state: AlertState
    state_since: datetime
    pending_state: AlertState | None
    pending_count: int
    last_value: Decimal | None
    fired: bool = False
    failed: bool = False
    disable_reason: str | None = None


# --- what creation reads ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class LpCandidate:
    """An open position found at creation, with what the sweep will need.

    The baseline -- in range or not, and where -- is what the confirmation
    shows, and becomes the alert's first state.
    """

    chain: str
    target: LpTarget
    state: AlertState
    tick: int
    price: Decimal
    price_lower: Decimal
    price_upper: Decimal
    base_symbol: str
    quote_symbol: str


@dataclass(frozen=True, slots=True)
class HealthCandidate:
    """An Aave account found at creation. `health_factor` is None with no debt."""

    chain: str
    health_factor: Decimal | None


@dataclass(frozen=True, slots=True)
class TargetsRead:
    """What one read of an address found, chain by chain.

    `unreachable` and `incomplete` are chain keys, kept apart from "nothing
    there": a chain that could not be read must not be confirmed as empty.
    """

    lp: tuple[LpCandidate, ...] = ()
    health: tuple[HealthCandidate, ...] = ()
    unreachable: tuple[str, ...] = ()
    #: Chains where some positions could not be listed.
    incomplete: tuple[str, ...] = ()


class AlertTargets(Protocol):
    async def read(self, address: str, kind: AlertKind, chain: str | None) -> TargetsRead:
        """Open positions or Aave accounts of an already-cleared address.

        `chain` None reads every chain. Never raises for a chain: a chain that
        fails is named in `unreachable`.
        """
        ...


class PositionObserver(Protocol):
    async def observe(self, alerts: Sequence[PositionAlert]) -> Mapping[int, Observation]:
        """A reading per alert id. An id left out is a failed reading."""
        ...


class AlertStore(Protocol):
    async def create(
        self, alert: NewAlert, first_check_at: datetime
    ) -> PositionAlert | AlertRefusal:
        """Store an alert, or say why not: at the cap, a duplicate, or nobody known."""
        ...

    async def for_person(self, person: PersonRef) -> Sequence[PositionAlert]:
        """Their own alerts, newest first. Never anybody else's."""
        ...

    async def delete(self, person: PersonRef, alert_id: int) -> bool:
        """Delete one of theirs; False when it is not theirs or not there.

        One answer for both, as for scheduled tasks: a caller that could tell
        them apart could learn that somebody else's alert exists.
        """
        ...

    async def claim_due(
        self, now: datetime, limit: int, interval_seconds: float
    ) -> Sequence[PositionAlert]:
        """Claim active alerts due at `now`, advancing each before it is read."""
        ...

    async def record(self, alert_id: int, update: AlertUpdate, now: datetime) -> None:
        """Record what a sweep decided."""
        ...

    async def disable(self, person: PersonRef, reason: str, now: datetime) -> int:
        """Stop every alert of a person who cannot be messaged. Returns how many."""
        ...
