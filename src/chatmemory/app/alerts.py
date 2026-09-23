"""Position alerts: the bounds on creating one, and what a sweep does with it.

Three pieces, and only the last one has side effects.

`AlertService` is the policy a person meets when an alert is made: the
health-factor bounds, a target that matches its kind, the per-person cap, and
only ever seeing or deleting your own. The store enforces the cap and the
bounds again; this is where a refusal becomes a reason.

`evaluate` is the whole decision, as a pure function of the stored alert, one
reading and the time. It is edge-triggered: a message goes out when the state
*changes*, never because it is still what it was. Three rules keep that from
turning into noise:

*   **Confirmation.** A range change counts only after `LP_CONFIRMATIONS`
    consecutive agreeing reads, so one block's wick across a range edge sends
    nothing. Aave counts on the first read below the limit, because that is
    the one that is urgent.
*   **Hysteresis.** A health factor that fell below the limit re-arms only at
    the limit plus `REARM_MARGIN`, so a factor hovering on the line is one
    message, not one per sweep.
*   **A failed read is not a reading.** It never changes the state and never
    sends anything; it is counted, so a listing can show that an alert has
    stopped being able to see its position.

A closed position -- no liquidity, or the NFT no longer in the watched wallet
-- is told once and the alert disabled, rather than watched in silence for
ever.

`AlertRunner` claims due alerts, reads them all in one batch, evaluates each,
and sends what fired through the same direct-message path scheduled tasks use.
No model is called anywhere on this path, and neither is `AskService`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from chatmemory.domain.chain import is_address, normalise
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.alerts import (
    CHAIN_NAMES,
    DIRECT_MESSAGES_CLOSED,
    HEALTH_CONFIRMATIONS,
    LP_CONFIRMATIONS,
    MAX_THRESHOLD,
    MIN_THRESHOLD,
    POSITION_CLOSED,
    REARM_MARGIN,
    AlertKind,
    AlertLanguage,
    AlertRefusal,
    AlertState,
    AlertStore,
    AlertUpdate,
    HealthObservation,
    LpObservation,
    NewAlert,
    Observation,
    PositionAlert,
    PositionObserver,
    ReadFailure,
)
from chatmemory.ports.notifications import DeliveryResult

if TYPE_CHECKING:
    # Only a type here: `schedules` imports the ask service, which imports the
    # alert request flow, which imports this module.
    from chatmemory.app.schedules import TaskMessenger

log = structlog.get_logger()

DEFAULT_BATCH = 200
"""Alerts claimed per sweep. Twenty people at their cap; the watcher's own
per-chain call cap is the real bound on a sweep's cost."""

DEFAULT_SWEEP_SECONDS = 300.0

NO_READING = ReadFailure("no reading")


# --- creating -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateResult:
    alert: PositionAlert | None = None
    refusal: AlertRefusal | None = None

    @property
    def created(self) -> bool:
        return self.alert is not None


def refusal_for(alert: NewAlert) -> AlertRefusal | None:
    """Why this alert may not be stored, or None. Checked before the store is asked."""
    if alert.chain not in CHAIN_NAMES or not is_address(alert.address):
        return AlertRefusal.INVALID_TARGET
    if alert.kind is AlertKind.LP_RANGE:
        ok = alert.lp is not None and alert.threshold is None
        return None if ok else AlertRefusal.INVALID_TARGET
    if alert.lp is not None or alert.threshold is None:
        return AlertRefusal.INVALID_TARGET
    if not MIN_THRESHOLD <= alert.threshold <= MAX_THRESHOLD:
        return AlertRefusal.THRESHOLD_OUT_OF_RANGE
    return None


class AlertService:
    """Creating, listing and deleting a person's own alerts."""

    def __init__(self, store: AlertStore, sweep_seconds: float = DEFAULT_SWEEP_SECONDS) -> None:
        self._store = store
        self._sweep = timedelta(seconds=sweep_seconds)

    async def create(self, alert: NewAlert, now: datetime | None = None) -> CreateResult:
        refused = refusal_for(alert)
        if refused is not None:
            return CreateResult(refusal=refused)
        cleaned = replace(alert, address=normalise(alert.address))
        # The first check is one sweep away: the baseline was read at creation,
        # and reading it again at once would only repeat it.
        first = (now or datetime.now(UTC)) + self._sweep
        stored = await self._store.create(cleaned, first)
        if isinstance(stored, AlertRefusal):
            return CreateResult(refusal=stored)
        return CreateResult(alert=stored)

    async def list_for(self, person: PersonRef) -> Sequence[PositionAlert]:
        return await self._store.for_person(person)

    async def delete(self, person: PersonRef, alert_id: int) -> bool:
        return await self._store.delete(person, alert_id)


# --- deciding -------------------------------------------------------------


class FiringKind(StrEnum):
    OUT_OF_RANGE = "out_of_range"
    BACK_IN_RANGE = "back_in_range"
    CLOSED = "closed"
    HEALTH_BELOW = "health_below"
    HEALTH_RECOVERED = "health_recovered"


@dataclass(frozen=True, slots=True)
class Firing:
    """One message's worth: what happened, to which alert, and what was read."""

    kind: FiringKind
    alert: PositionAlert
    observation: LpObservation | HealthObservation


@dataclass(frozen=True, slots=True)
class Evaluation:
    update: AlertUpdate
    firing: Firing | None = None


def evaluate(alert: PositionAlert, observation: Observation, now: datetime) -> Evaluation:
    """The next state of `alert` given one reading, and the message it earns."""
    if isinstance(observation, LpObservation) and alert.kind is AlertKind.LP_RANGE:
        return _evaluate_lp(alert, observation, now)
    if isinstance(observation, HealthObservation) and alert.kind is AlertKind.AAVE_HEALTH:
        return _evaluate_health(alert, observation, now)
    # A failure, or a reading of the wrong shape for this alert: either way
    # nothing is known, so nothing changes.
    return Evaluation(_unchanged(alert, failed=True))


def _unchanged(alert: PositionAlert, *, failed: bool = False) -> AlertUpdate:
    return AlertUpdate(
        state=alert.state,
        state_since=alert.state_since,
        pending_state=alert.pending_state,
        pending_count=alert.pending_count,
        last_value=alert.last_value,
        failed=failed,
    )


def _advance(
    alert: PositionAlert,
    observed: AlertState,
    value: Decimal | None,
    now: datetime,
    confirmations: int,
) -> tuple[AlertUpdate, bool]:
    """The state machine both kinds share. Returns the update and whether the
    state just changed (as opposed to being set for the first time)."""
    if alert.state is AlertState.UNKNOWN:
        # No baseline was taken at creation: this reading is it, silently.
        return AlertUpdate(observed, now, None, 0, value), False
    if observed is alert.state:
        return AlertUpdate(alert.state, alert.state_since, None, 0, value), False
    count = alert.pending_count + 1 if alert.pending_state is observed else 1
    if count < confirmations:
        return AlertUpdate(alert.state, alert.state_since, observed, count, value), False
    return AlertUpdate(observed, now, None, 0, value), True


def _evaluate_lp(alert: PositionAlert, reading: LpObservation, now: datetime) -> Evaluation:
    observed = reading.state
    value = Decimal(reading.tick) if observed is not AlertState.CLOSED else alert.last_value
    update, changed = _advance(alert, observed, value, now, LP_CONFIRMATIONS)
    if observed is AlertState.CLOSED and update.state is AlertState.CLOSED:
        # Told once, then stopped: a closed position has nothing left to watch.
        closing = _with(update, fired=True, disable_reason=POSITION_CLOSED)
        return Evaluation(closing, Firing(FiringKind.CLOSED, alert, reading))
    if not changed:
        return Evaluation(update)
    if observed is AlertState.OUT_OF_RANGE:
        return _fire(update, FiringKind.OUT_OF_RANGE, alert, reading)
    if alert.notify_return:
        return _fire(update, FiringKind.BACK_IN_RANGE, alert, reading)
    return Evaluation(update)


def health_state(alert: PositionAlert, health: Decimal | None) -> AlertState:
    """OK, BELOW or NO_DEBT, with the re-arm band applied to a fired alert."""
    if health is None:
        return AlertState.NO_DEBT
    threshold = alert.threshold or MIN_THRESHOLD
    if health < threshold:
        return AlertState.BELOW
    if alert.state is AlertState.BELOW and health < threshold + REARM_MARGIN:
        # Back above the limit but inside the band: still fired, not re-armed.
        return AlertState.BELOW
    return AlertState.OK


def _evaluate_health(
    alert: PositionAlert, reading: HealthObservation, now: datetime
) -> Evaluation:
    observed = health_state(alert, reading.health_factor)
    update, changed = _advance(alert, observed, reading.health_factor, now, HEALTH_CONFIRMATIONS)
    if not changed:
        return Evaluation(update)
    if observed is AlertState.BELOW:
        return _fire(update, FiringKind.HEALTH_BELOW, alert, reading)
    if alert.state is AlertState.BELOW and alert.notify_return:
        return _fire(update, FiringKind.HEALTH_RECOVERED, alert, reading)
    # OK and no debt are both "not below": moving between them is not news.
    return Evaluation(update)


def _fire(
    update: AlertUpdate,
    kind: FiringKind,
    alert: PositionAlert,
    reading: LpObservation | HealthObservation,
) -> Evaluation:
    return Evaluation(_with(update, fired=True), Firing(kind, alert, reading))


def _with(update: AlertUpdate, *, fired: bool, disable_reason: str | None = None) -> AlertUpdate:
    return AlertUpdate(
        state=update.state,
        state_since=update.state_since,
        pending_state=update.pending_state,
        pending_count=update.pending_count,
        last_value=update.last_value,
        fired=fired,
        disable_reason=disable_reason,
    )


# --- saying ---------------------------------------------------------------

PREFIX = {AlertLanguage.ENGLISH: "🔔 **Alert**", AlertLanguage.PORTUGUESE: "🔔 **Alerta**"}

_LP = {
    AlertLanguage.ENGLISH: {
        FiringKind.OUT_OF_RANGE: (
            "your {position} on {chain} is **out of range**: price {price} {quote} per "
            "{base}, range {low} – {high}. It earns no fees until the price returns."
        ),
        FiringKind.BACK_IN_RANGE: (
            "your {position} on {chain} is **back in range** (price {price} {quote} per {base})."
        ),
        FiringKind.CLOSED: (
            "your {position} on {chain} is closed or no longer in this wallet, "
            "so I stopped watching it."
        ),
    },
    AlertLanguage.PORTUGUESE: {
        FiringKind.OUT_OF_RANGE: (
            "sua posição {position} na {chain} **saiu da faixa**: preço {price} {quote} por "
            "{base}, faixa {low} – {high}. Ela não rende taxas até o preço voltar."
        ),
        FiringKind.BACK_IN_RANGE: (
            "sua posição {position} na {chain} **voltou para a faixa** "
            "(preço {price} {quote} por {base})."
        ),
        FiringKind.CLOSED: (
            "sua posição {position} na {chain} foi fechada ou não está mais nesta "
            "carteira; parei de acompanhá-la."
        ),
    },
}

_HEALTH = {
    AlertLanguage.ENGLISH: {
        FiringKind.HEALTH_BELOW: (
            "your Aave health factor on {chain} dropped to **{health}** (your limit "
            "{limit}). Collateral {collateral}, debt {debt}. Below 1.0 the position can be "
            "liquidated. I check every {minutes} minutes, so this is not liquidation "
            "protection."
        ),
        FiringKind.HEALTH_RECOVERED: (
            "your Aave health factor on {chain} is back to **{health}** (limit {limit})."
        ),
        "no_debt": "your Aave position on {chain} has no debt any more (limit {limit}).",
    },
    AlertLanguage.PORTUGUESE: {
        FiringKind.HEALTH_BELOW: (
            "seu health factor no Aave na {chain} caiu para **{health}** (seu limite "
            "{limit}). Colateral {collateral}, dívida {debt}. Abaixo de 1,0 a posição pode "
            "ser liquidada. Eu verifico a cada {minutes} minutos, então isso não é "
            "proteção contra liquidação."
        ),
        FiringKind.HEALTH_RECOVERED: (
            "seu health factor no Aave na {chain} voltou para **{health}** (limite {limit})."
        ),
        "no_debt": "sua posição no Aave na {chain} não tem mais dívida (limite {limit}).",
    },
}


def number(value: Decimal, language: AlertLanguage, places: int = 2) -> str:
    """A figure in the reader's notation: 3,160.20 or 3.160,20."""
    text = f"{value:.6g}" if value and abs(value) < 1 else f"{value:,.{places}f}"
    if language is AlertLanguage.PORTUGUESE:
        text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text


def dollars(value: Decimal, language: AlertLanguage) -> str:
    sign = "US$ " if language is AlertLanguage.PORTUGUESE else "$"
    return sign + number(value, language)


def fee_tier(fee: int, language: AlertLanguage) -> str:
    if fee & 0x800000:
        return "taxa dinâmica" if language is AlertLanguage.PORTUGUESE else "dynamic fee"
    tier = f"{Decimal(fee) / 10000:f}".rstrip("0").rstrip(".")
    if language is AlertLanguage.PORTUGUESE:
        tier = tier.replace(".", ",")
    return tier + "%"


def _position(alert: PositionAlert, language: AlertLanguage) -> str:
    lp = alert.lp
    if lp is None:
        return f"#{alert.id}"
    pair = f"{lp.token0_symbol}/{lp.token1_symbol}"
    fee = fee_tier(lp.fee, language)
    if language is AlertLanguage.PORTUGUESE:
        return f"{lp.protocol.label} {pair} {fee} #{lp.token_id}"
    return f"{lp.protocol.label} {pair} {fee} position #{lp.token_id}"


def _render_lp(firing: Firing, reading: LpObservation) -> str:
    alert, lang = firing.alert, firing.alert.language
    return _LP[lang][firing.kind].format(
        position=_position(alert, lang),
        chain=alert.chain_name,
        price=number(reading.price, lang),
        low=number(reading.price_lower, lang),
        high=number(reading.price_upper, lang),
        base=reading.base_symbol,
        quote=reading.quote_symbol,
    )


def _render_health(firing: Firing, reading: HealthObservation, minutes: int) -> str:
    alert, lang = firing.alert, firing.alert.language
    templates = _HEALTH[lang]
    key: str = firing.kind
    if reading.health_factor is None:
        key = "no_debt"
    return templates[key].format(
        chain=alert.chain_name,
        health=number(reading.health_factor or Decimal(0), lang),
        limit=number(alert.threshold or Decimal(0), lang),
        collateral=dollars(reading.collateral_usd, lang),
        debt=dollars(reading.debt_usd, lang),
        minutes=minutes,
    )


def render_alert(firing: Firing, sweep_seconds: float = DEFAULT_SWEEP_SECONDS) -> str:
    """The direct message, in the language fixed when the alert was made.

    The last line names the commands that list and stop it, with this alert's
    number, so the way out is in the message that might prompt it.
    """
    reading = firing.observation
    if isinstance(reading, LpObservation):
        body = _render_lp(firing, reading)
    else:
        body = _render_health(firing, reading, max(1, round(sweep_seconds / 60)))
    commands = f"-# `/alert list` · `/alert delete {firing.alert.id}`"
    return f"{PREFIX[firing.alert.language]} — {body}\n{commands}"


# --- running --------------------------------------------------------------


class AlertRunner:
    """Claims due alerts, reads them in one batch, and sends what changed."""

    def __init__(
        self,
        store: AlertStore,
        observer: PositionObserver,
        messenger: TaskMessenger,
        *,
        sweep_seconds: float = DEFAULT_SWEEP_SECONDS,
        batch: int = DEFAULT_BATCH,
    ) -> None:
        self._store = store
        self._observer = observer
        self._messenger = messenger
        self._sweep = sweep_seconds
        self._batch = batch

    async def run_due(self, now: datetime | None = None) -> int:
        """Check every alert due now. Returns how many messages were sent."""
        moment = now or datetime.now(UTC)
        due = await self._store.claim_due(moment, self._batch, self._sweep)
        if not due:
            return 0
        readings = await self._read(due)
        closed: set[PersonRef] = set()
        sent = 0
        for alert in due:
            if alert.person in closed:
                # Their direct messages closed earlier in this sweep, and
                # every alert of theirs was disabled with it.
                continue
            if await self._check_one(alert, readings.get(alert.id, NO_READING), moment, closed):
                sent += 1
        return sent

    async def _read(self, due: Sequence[PositionAlert]) -> Mapping[int, Observation]:
        try:
            return await self._observer.observe(due)
        except Exception as exc:  # noqa: BLE001 - a failed read is a failure per alert
            log.warning("alerts.read_failed", alerts=len(due), error=str(exc)[:200])
            return {}

    async def _check_one(
        self, alert: PositionAlert, reading: Observation, now: datetime, closed: set[PersonRef]
    ) -> bool:
        evaluation = evaluate(alert, reading, now)
        if evaluation.firing is None:
            await self._store.record(alert.id, evaluation.update, now)
            return False
        text = render_alert(evaluation.firing, self._sweep)
        result = await self._messenger.deliver(alert.person, alert.id, text)
        if result is DeliveryResult.SENT:
            await self._store.record(alert.id, evaluation.update, now)
            log.info("alerts.fired", alert_id=alert.id, kind=str(evaluation.firing.kind))
            return True
        if result is DeliveryResult.FAILED:
            # A Discord blip, not a refusal. Nothing is recorded, so the
            # stored state still predates the change and the next sweep
            # finds it again and retries the message.
            log.warning("alerts.not_delivered", alert_id=alert.id)
            return False
        # Their direct messages are closed. Every alert of theirs stops, as
        # scheduled tasks do: the obstacle is the person's settings, not
        # this alert. The reading is still recorded: it was true whether or
        # not it could be told.
        await self._store.record(alert.id, _with(evaluation.update, fired=False), now)
        stopped = await self._store.disable(alert.person, DIRECT_MESSAGES_CLOSED, now)
        closed.add(alert.person)
        log.info("alerts.disabled", person=str(alert.person), alerts=stopped)
        return False
