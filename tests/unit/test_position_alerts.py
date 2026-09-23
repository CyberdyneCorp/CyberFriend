"""Position alerts: the edge trigger, the messages, the bounds and the sweep.

`evaluate` is pure, so every transition is a table row here: confirmation for a
range change, hysteresis for a health factor, a closed position told once, and
a failed read that neither changes anything nor says anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from chatmemory.app.alerts import (
    AlertRunner,
    AlertService,
    FiringKind,
    evaluate,
    number,
    render_alert,
)
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.alerts import (
    DIRECT_MESSAGES_CLOSED,
    POSITION_CLOSED,
    AddressSource,
    AlertKind,
    AlertLanguage,
    AlertRefusal,
    AlertState,
    AlertUpdate,
    HealthObservation,
    LpObservation,
    LpProtocol,
    LpTarget,
    NewAlert,
    Observation,
    PositionAlert,
    ReadFailure,
)
from chatmemory.ports.notifications import DeliveryResult

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=3)
LEO = PersonRef("discord", 7)
WALLET = "0xdd8a0000000000000000000000000000000063d6"

LP = LpTarget(
    protocol=LpProtocol.UNISWAP_V3,
    token_id=4558452,
    pool_ref="0xd0b53d9277642d899df5c87a3966a349a798f224",
    token0_symbol="WETH",
    token1_symbol="USDC",
    token0_decimals=18,
    token1_decimals=6,
    fee=500,
)


def lp_alert(state: AlertState = AlertState.IN_RANGE, **changes: Any) -> PositionAlert:
    alert = PositionAlert(
        id=7,
        person=LEO,
        kind=AlertKind.LP_RANGE,
        chain="base",
        address=WALLET,
        language=AlertLanguage.ENGLISH,
        state=state,
        state_since=EARLIER,
        created_at=EARLIER,
        next_check_at=NOW,
        lp=LP,
    )
    return replace(alert, **changes)


def health_alert(state: AlertState = AlertState.OK, **changes: Any) -> PositionAlert:
    alert = PositionAlert(
        id=8,
        person=LEO,
        kind=AlertKind.AAVE_HEALTH,
        chain="base",
        address=WALLET,
        language=AlertLanguage.ENGLISH,
        state=state,
        state_since=EARLIER,
        created_at=EARLIER,
        next_check_at=NOW,
        threshold=Decimal("1.30"),
    )
    return replace(alert, **changes)


def reading(tick: int, *, liquidity: int = 10**15, owned: bool = True) -> LpObservation:
    return LpObservation(
        owned=owned,
        liquidity=liquidity,
        tick=tick,
        tick_lower=-199560,
        tick_upper=-195770,
        price=Decimal("3160.2"),
        price_lower=Decimal("2156.02"),
        price_upper=Decimal("3149.5"),
        base_symbol="WETH",
        quote_symbol="USDC",
    )


IN = reading(-197404)
OUT = reading(-195000)


def hf(value: str | None) -> HealthObservation:
    return HealthObservation(
        health_factor=None if value is None else Decimal(value),
        collateral_usd=Decimal("27699.05"),
        debt_usd=Decimal("15221.12"),
    )


# --- range alerts -------------------------------------------------------------


def test_one_read_out_of_range_is_pending_and_says_nothing() -> None:
    result = evaluate(lp_alert(), OUT, NOW)

    assert result.firing is None
    assert result.update.state is AlertState.IN_RANGE
    assert result.update.pending_state is AlertState.OUT_OF_RANGE
    assert result.update.pending_count == 1
    assert result.update.state_since == EARLIER


def test_the_second_agreeing_read_fires_out_of_range() -> None:
    pending = lp_alert(pending_state=AlertState.OUT_OF_RANGE, pending_count=1)

    result = evaluate(pending, OUT, NOW)

    assert result.firing is not None and result.firing.kind is FiringKind.OUT_OF_RANGE
    assert result.update.state is AlertState.OUT_OF_RANGE
    assert result.update.state_since == NOW
    assert (result.update.pending_state, result.update.pending_count) == (None, 0)
    assert result.update.fired


def test_a_one_read_wick_is_forgotten_when_the_next_read_is_back_in_range() -> None:
    pending = lp_alert(pending_state=AlertState.OUT_OF_RANGE, pending_count=1)

    result = evaluate(pending, IN, NOW)

    assert result.firing is None
    assert result.update.state is AlertState.IN_RANGE
    assert (result.update.pending_state, result.update.pending_count) == (None, 0)


def test_still_out_of_range_says_nothing() -> None:
    result = evaluate(lp_alert(AlertState.OUT_OF_RANGE), OUT, NOW)

    assert result.firing is None
    assert not result.update.fired
    assert result.update.state is AlertState.OUT_OF_RANGE


def test_back_in_range_fires_after_confirmation() -> None:
    first = evaluate(lp_alert(AlertState.OUT_OF_RANGE), IN, NOW)
    assert first.firing is None
    pending = lp_alert(
        AlertState.OUT_OF_RANGE, pending_state=AlertState.IN_RANGE, pending_count=1
    )

    second = evaluate(pending, IN, NOW)

    assert second.firing is not None and second.firing.kind is FiringKind.BACK_IN_RANGE
    assert second.update.state is AlertState.IN_RANGE


def test_back_in_range_is_silent_when_the_person_turned_it_off() -> None:
    pending = lp_alert(
        AlertState.OUT_OF_RANGE,
        pending_state=AlertState.IN_RANGE,
        pending_count=1,
        notify_return=False,
    )

    result = evaluate(pending, IN, NOW)

    assert result.firing is None
    assert result.update.state is AlertState.IN_RANGE, "the state still moves"


@pytest.mark.parametrize(
    "closed",
    [reading(-197404, liquidity=0), reading(-197404, owned=False)],
    ids=["no-liquidity", "not-owned"],
)
def test_a_closed_position_is_told_once_and_the_alert_disabled(closed: LpObservation) -> None:
    first = evaluate(lp_alert(), closed, NOW)
    assert first.firing is None, "closing is confirmed like any other change"
    pending = lp_alert(pending_state=AlertState.CLOSED, pending_count=1)

    result = evaluate(pending, closed, NOW)

    assert result.firing is not None and result.firing.kind is FiringKind.CLOSED
    assert result.update.state is AlertState.CLOSED
    assert result.update.disable_reason == POSITION_CLOSED


def test_an_alert_with_no_baseline_takes_the_first_reading_silently() -> None:
    result = evaluate(lp_alert(AlertState.UNKNOWN), OUT, NOW)

    assert result.firing is None
    assert result.update.state is AlertState.OUT_OF_RANGE


@pytest.mark.parametrize(
    "alert",
    [
        lp_alert(pending_state=AlertState.OUT_OF_RANGE, pending_count=1),
        health_alert(AlertState.BELOW),
    ],
    ids=["lp", "health"],
)
def test_a_failed_read_changes_nothing_and_says_nothing(alert: PositionAlert) -> None:
    result = evaluate(alert, ReadFailure("could not be reached"), NOW)

    assert result.firing is None
    assert result.update.failed
    assert result.update.state is alert.state
    assert result.update.state_since == alert.state_since
    assert result.update.pending_state is alert.pending_state
    assert result.update.pending_count == alert.pending_count
    assert result.update.disable_reason is None


def test_a_reading_of_the_wrong_kind_counts_as_a_failure() -> None:
    result = evaluate(lp_alert(), hf("1.1"), NOW)

    assert result.firing is None and result.update.failed


# --- health alerts ------------------------------------------------------------


def test_the_first_read_below_the_limit_fires() -> None:
    result = evaluate(health_alert(), hf("1.28"), NOW)

    assert result.firing is not None and result.firing.kind is FiringKind.HEALTH_BELOW
    assert result.update.state is AlertState.BELOW
    assert result.update.last_value == Decimal("1.28")


def test_below_and_still_below_says_nothing() -> None:
    result = evaluate(health_alert(AlertState.BELOW), hf("1.25"), NOW)

    assert result.firing is None and result.update.state is AlertState.BELOW


@pytest.mark.parametrize("value", ["1.30", "1.32", "1.3499"])
def test_inside_the_rearm_band_it_stays_fired(value: str) -> None:
    result = evaluate(health_alert(AlertState.BELOW), hf(value), NOW)

    assert result.firing is None
    assert result.update.state is AlertState.BELOW


def test_at_the_limit_plus_the_margin_it_rearms_and_says_so() -> None:
    result = evaluate(health_alert(AlertState.BELOW), hf("1.35"), NOW)

    assert result.firing is not None and result.firing.kind is FiringKind.HEALTH_RECOVERED
    assert result.update.state is AlertState.OK


def test_an_armed_alert_just_above_the_limit_is_not_below() -> None:
    result = evaluate(health_alert(AlertState.OK), hf("1.31"), NOW)

    assert result.firing is None and result.update.state is AlertState.OK


def test_recovery_is_silent_when_the_person_turned_it_off() -> None:
    result = evaluate(health_alert(AlertState.BELOW, notify_return=False), hf("1.5"), NOW)

    assert result.firing is None and result.update.state is AlertState.OK


def test_repaying_the_debt_recovers_a_fired_alert() -> None:
    result = evaluate(health_alert(AlertState.BELOW), hf(None), NOW)

    assert result.firing is not None and result.firing.kind is FiringKind.HEALTH_RECOVERED
    assert result.update.state is AlertState.NO_DEBT
    assert result.update.last_value is None


def test_no_debt_and_ok_are_both_not_below() -> None:
    to_no_debt = evaluate(health_alert(AlertState.OK), hf(None), NOW)
    to_ok = evaluate(health_alert(AlertState.NO_DEBT), hf("2.0"), NOW)

    assert to_no_debt.firing is None and to_no_debt.update.state is AlertState.NO_DEBT
    assert to_ok.firing is None and to_ok.update.state is AlertState.OK


def test_borrowing_from_no_debt_straight_below_the_limit_fires() -> None:
    result = evaluate(health_alert(AlertState.NO_DEBT), hf("1.1"), NOW)

    assert result.firing is not None and result.firing.kind is FiringKind.HEALTH_BELOW


# --- the messages -------------------------------------------------------------


def _fired(alert: PositionAlert, observation: Observation) -> str:
    result = evaluate(alert, observation, NOW)
    assert result.firing is not None
    return render_alert(result.firing)


def test_out_of_range_in_english() -> None:
    alert = lp_alert(pending_state=AlertState.OUT_OF_RANGE, pending_count=1)

    assert _fired(alert, OUT) == (
        "🔔 **Alert** — your Uniswap v3 WETH/USDC 0.05% position #4558452 on Base is "
        "**out of range**: price 3,160.20 USDC per WETH, range 2,156.02 – 3,149.50. "
        "It earns no fees until the price returns."
    )


def test_out_of_range_in_portuguese() -> None:
    alert = lp_alert(
        pending_state=AlertState.OUT_OF_RANGE,
        pending_count=1,
        language=AlertLanguage.PORTUGUESE,
    )

    assert _fired(alert, OUT) == (
        "🔔 **Alerta** — sua posição Uniswap v3 WETH/USDC 0,05% #4558452 na Base "
        "**saiu da faixa**: preço 3.160,20 USDC por WETH, faixa 2.156,02 – 3.149,50. "
        "Ela não rende taxas até o preço voltar."
    )


def test_back_in_range_and_closed_in_both_languages() -> None:
    back = lp_alert(AlertState.OUT_OF_RANGE, pending_state=AlertState.IN_RANGE, pending_count=1)
    closed = lp_alert(pending_state=AlertState.CLOSED, pending_count=1)
    gone = reading(0, liquidity=0)

    assert "is **back in range** (price 3,160.20 USDC per WETH)." in _fired(back, IN)
    assert "**voltou para a faixa** (preço 3.160,20 USDC por WETH)." in _fired(
        replace(back, language=AlertLanguage.PORTUGUESE), IN
    )
    assert "is closed or no longer in this wallet, so I stopped watching it." in _fired(
        closed, gone
    )
    assert "foi fechada ou não está mais nesta carteira; parei de acompanhá-la." in _fired(
        replace(closed, language=AlertLanguage.PORTUGUESE), gone
    )


def test_health_below_in_both_languages() -> None:
    english = _fired(health_alert(), hf("1.28"))
    portuguese = _fired(health_alert(language=AlertLanguage.PORTUGUESE), hf("1.28"))

    assert english == (
        "🔔 **Alert** — your Aave health factor on Base dropped to **1.28** (your limit "
        "1.30). Collateral $27,699.05, debt $15,221.12. Below 1.0 the position can be "
        "liquidated. I check every 5 minutes, so this is not liquidation protection."
    )
    assert portuguese == (
        "🔔 **Alerta** — seu health factor no Aave na Base caiu para **1,28** (seu limite "
        "1,30). Colateral US$ 27.699,05, dívida US$ 15.221,12. Abaixo de 1,0 a posição "
        "pode ser liquidada. Eu verifico a cada 5 minutos, então isso não é proteção "
        "contra liquidação."
    )


def test_recovered_in_both_languages() -> None:
    fired = health_alert(AlertState.BELOW)

    assert _fired(fired, hf("1.36")).endswith("is back to **1.36** (limit 1.30).")
    assert _fired(replace(fired, language=AlertLanguage.PORTUGUESE), hf("1.36")).endswith(
        "voltou para **1,36** (limite 1,30)."
    )
    assert "has no debt any more" in _fired(fired, hf(None))


def test_small_prices_keep_their_significant_digits() -> None:
    assert number(Decimal("0.000371234"), AlertLanguage.ENGLISH) == "0.000371234"
    assert number(Decimal("0.000371234"), AlertLanguage.PORTUGUESE) == "0,000371234"


def test_no_message_names_a_command_that_does_not_exist_yet() -> None:
    """`/alert` arrives with the commands; until then a DM must not point at it."""
    texts = [
        _fired(lp_alert(pending_state=AlertState.OUT_OF_RANGE, pending_count=1), OUT),
        _fired(health_alert(), hf("1.28")),
    ]
    assert not any("/alert" in t for t in texts)


# --- creating ------------------------------------------------------------------


class MemoryStore:
    """Enough of `AlertStore` for the service and the runner."""

    def __init__(self, alerts: Sequence[PositionAlert] = ()) -> None:
        self.alerts = list(alerts)
        self.created: list[tuple[NewAlert, datetime]] = []
        self.records: list[tuple[int, AlertUpdate]] = []
        self.disabled: list[tuple[PersonRef, str]] = []
        self.claims: list[tuple[datetime, float]] = []

    async def create(
        self, alert: NewAlert, first_check_at: datetime
    ) -> PositionAlert | AlertRefusal:
        self.created.append((alert, first_check_at))
        return lp_alert(address=alert.address)

    async def for_person(self, person: PersonRef) -> Sequence[PositionAlert]:
        return [a for a in self.alerts if a.person == person]

    async def delete(self, person: PersonRef, alert_id: int) -> bool:
        return False

    async def claim_due(
        self, now: datetime, limit: int, interval_seconds: float
    ) -> Sequence[PositionAlert]:
        self.claims.append((now, interval_seconds))
        return self.alerts

    async def record(self, alert_id: int, update: AlertUpdate, now: datetime) -> None:
        self.records.append((alert_id, update))

    async def disable(self, person: PersonRef, reason: str, now: datetime) -> int:
        self.disabled.append((person, reason))
        return 1


def new_health(threshold: str) -> NewAlert:
    return NewAlert(
        person=LEO,
        kind=AlertKind.AAVE_HEALTH,
        chain="base",
        address=WALLET.upper().replace("0X", "0x"),
        address_source=AddressSource.SAVED,
        language=AlertLanguage.ENGLISH,
        threshold=Decimal(threshold),
    )


@pytest.mark.parametrize("threshold", ["1.04", "5.01", "0.5"])
async def test_a_threshold_outside_the_bounds_is_refused(threshold: str) -> None:
    store = MemoryStore()

    result = await AlertService(store).create(new_health(threshold), NOW)

    assert result.refusal is AlertRefusal.THRESHOLD_OUT_OF_RANGE
    assert store.created == []


@pytest.mark.parametrize("threshold", ["1.05", "5.0"])
async def test_the_bounds_themselves_are_allowed(threshold: str) -> None:
    result = await AlertService(MemoryStore()).create(new_health(threshold), NOW)

    assert result.created


@pytest.mark.parametrize(
    "alert",
    [
        replace(new_health("1.3"), chain="solana"),
        replace(new_health("1.3"), address="0x123"),
        replace(new_health("1.3"), lp=LP),
        replace(new_health("1.3"), kind=AlertKind.LP_RANGE),
        replace(new_health("1.3"), kind=AlertKind.LP_RANGE, threshold=None),
    ],
    ids=["chain", "address", "health-with-position", "lp-with-threshold", "lp-without-target"],
)
async def test_a_target_that_does_not_match_its_kind_is_refused(alert: NewAlert) -> None:
    store = MemoryStore()

    result = await AlertService(store).create(alert, NOW)

    assert result.refusal is AlertRefusal.INVALID_TARGET
    assert store.created == []


async def test_creation_stores_the_address_lowercase_and_checks_one_sweep_later() -> None:
    store = MemoryStore()

    await AlertService(store, sweep_seconds=300).create(new_health("1.3"), NOW)

    [(stored, first)] = store.created
    assert stored.address == WALLET
    assert first == NOW + timedelta(seconds=300)


# --- the sweep ------------------------------------------------------------------


class FakeObserver:
    def __init__(self, readings: Mapping[int, Observation] | Exception) -> None:
        self.readings = readings
        self.seen: list[Sequence[PositionAlert]] = []

    async def observe(self, alerts: Sequence[PositionAlert]) -> Mapping[int, Observation]:
        self.seen.append(alerts)
        if isinstance(self.readings, Exception):
            raise self.readings
        return self.readings


class FakeMessenger:
    def __init__(self, result: DeliveryResult = DeliveryResult.SENT) -> None:
        self.result = result
        self.sent: list[tuple[PersonRef, int, str]] = []

    async def deliver(self, person: PersonRef, task_id: int, text: str) -> DeliveryResult:
        if self.result is DeliveryResult.SENT:
            self.sent.append((person, task_id, text))
        return self.result


async def test_the_sweep_reads_everything_due_in_one_batch_and_sends_what_changed() -> None:
    fired = health_alert()
    quiet = lp_alert()
    store = MemoryStore([fired, quiet])
    observer = FakeObserver({fired.id: hf("1.2"), quiet.id: IN})
    messenger = FakeMessenger()

    sent = await AlertRunner(store, observer, messenger, sweep_seconds=300).run_due(NOW)

    assert sent == 1
    assert observer.seen == [[fired, quiet]]
    assert [(p, i) for p, i, _ in messenger.sent] == [(LEO, fired.id)]
    assert store.claims == [(NOW, 300)]
    assert {i for i, _ in store.records} == {fired.id, quiet.id}


async def test_a_reading_that_is_missing_is_a_failure_not_a_state() -> None:
    alert = lp_alert()
    store = MemoryStore([alert])

    await AlertRunner(store, FakeObserver({}), FakeMessenger()).run_due(NOW)

    [(_, update)] = store.records
    assert update.failed and update.state is AlertState.IN_RANGE


async def test_an_observer_that_raises_fails_every_alert_and_sends_nothing() -> None:
    store = MemoryStore([health_alert(), lp_alert()])
    messenger = FakeMessenger()

    sent = await AlertRunner(store, FakeObserver(RuntimeError("boom")), messenger).run_due(NOW)

    assert sent == 0 and messenger.sent == []
    assert len(store.records) == 2
    assert all(update.failed for _, update in store.records)


async def test_closed_direct_messages_stop_every_alert_of_that_person() -> None:
    first = health_alert()
    second = replace(health_alert(), id=9)
    store = MemoryStore([first, second])
    observer = FakeObserver({first.id: hf("1.1"), second.id: hf("1.1")})

    sent = await AlertRunner(store, observer, FakeMessenger(DeliveryResult.CLOSED)).run_due(NOW)

    assert sent == 0
    assert store.disabled == [(LEO, DIRECT_MESSAGES_CLOSED)], "disabled once, for the person"
    assert [i for i, _ in store.records] == [first.id]
    assert not store.records[0][1].fired


async def test_a_transient_send_failure_stops_nothing_and_retries_next_sweep() -> None:
    """A Discord blip is not a closed door: disabling on it would silence a
    liquidation warning for good, under a reason that is not true."""
    alert = health_alert()
    store = MemoryStore([alert])
    observer = FakeObserver({alert.id: hf("1.1")})

    sent = await AlertRunner(store, observer, FakeMessenger(DeliveryResult.FAILED)).run_due(NOW)

    assert sent == 0
    assert store.disabled == []
    assert store.records == [], "the change stays unrecorded, so it is found again"

    messenger = FakeMessenger()
    assert await AlertRunner(store, observer, messenger).run_due(NOW) == 1
    assert [i for _, i, _ in messenger.sent] == [alert.id]


async def test_nothing_due_reads_nothing() -> None:
    observer = FakeObserver({})

    assert await AlertRunner(MemoryStore(), observer, FakeMessenger()).run_due(NOW) == 0
    assert observer.seen == []
