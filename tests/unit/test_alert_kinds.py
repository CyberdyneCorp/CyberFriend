"""Price alerts and near-edge range alerts: the words, the numbers, the transitions.

Both kinds ride on the position-alert engine, so what is pinned here is only
what they add: recognising "avisa quando o BTC passar de 100k" and "warn me
within 5% of the range edge" (and nothing that merely asks a price), reading a
level as either notation writes it, the crossing and the near-edge band with
their hysteresis, the distance from the edge on the live Arbitrum position,
the confirmation's baseline, the messages, and one price request per sweep.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from chatmemory.adapters.chain.watch import lp_observation
from chatmemory.adapters.discord.alerts import alert_listing
from chatmemory.adapters.market.alert_prices import AlertPrices
from chatmemory.adapters.market.coingecko import CoinGeckoProvider
from chatmemory.adapters.market.provider import ProviderUnavailable
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.app.alert_intent import AlertIntent, alert_intent, parse_amount
from chatmemory.app.alert_requests import AlertRequests, alert_language
from chatmemory.app.alerts import (
    AlertRunner,
    AlertService,
    FiringKind,
    evaluate,
    render_alert,
)
from chatmemory.app.egress import CRYPTO_ASSETS
from chatmemory.ports.alerts import (
    PRICE_ASSETS,
    AlertKind,
    AlertLanguage,
    AlertRefusal,
    AlertState,
    Edge,
    LpCandidate,
    LpObservation,
    LpProtocol,
    LpTarget,
    NewAlert,
    PositionAlert,
    PriceDirection,
    PriceObservation,
    PriceTarget,
    ReadFailure,
    TargetsRead,
    edge_distance,
)
from tests.unit.test_alert_requests import LEO, SAVED, Chain, Store
from tests.unit.test_position_alerts import FakeMessenger, FakeObserver, MemoryStore

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)
EN, PT = AlertLanguage.ENGLISH, AlertLanguage.PORTUGUESE
ABOVE, BELOW = PriceDirection.ABOVE, PriceDirection.BELOW
WALLET = "0xdd8a0000000000000000000000000000000063d6"

# The live Arbitrum v4 position the design was read against.
V4 = LpTarget(LpProtocol.UNISWAP_V4, 210171, "0xpoolid", "ETH", "USDC", 18, 6, 500)
TICK, LOWER, UPPER = -197404, -197920, -197070


def arbitrum(tick: int = TICK) -> LpObservation:
    return lp_observation(V4, liquidity=10**14, tick=tick, lower=LOWER, upper=UPPER)


# --- recognising --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "asset", "direction", "level"),
    [
        ("avisa quando o BTC passar de 100k", "BTC", ABOVE, "100000"),
        ("me avisa se o ETH cair abaixo de 2500", "ETH", BELOW, "2500"),
        ("alert me when ETH goes above $3,000", "ETH", ABOVE, "3000"),
        ("notify me if bitcoin drops below 90k", "BTC", BELOW, "90000"),
        ("ping me if eth falls under 2,750.50", "ETH", BELOW, "2750.50"),
        ("me avisa quando o bitcoin ultrapassar 120 mil", "BTC", ABOVE, "120000"),
        ("me avise se o ether ficar abaixo de 2.500", "ETH", BELOW, "2500"),
        ("can you tell me when ETH rises past 3k?", "ETH", ABOVE, "3000"),
        # A level with no side: the proposal settles it from the price now.
        ("tell me when BTC hits 100k", "BTC", None, "100000"),
        ("me avisa quando o eth chegar a 2.500", "ETH", None, "2500"),
    ],
)
def test_price_requests_are_recognised_in_both_languages(
    text: str, asset: str, direction: PriceDirection | None, level: str
) -> None:
    found = alert_intent(text)
    assert found == AlertIntent(
        AlertKind.PRICE, asset=asset, direction=direction, level=Decimal(level)
    )


def test_a_price_request_with_no_level_is_still_one() -> None:
    found = alert_intent("avisa quando o BTC subir")
    assert found is not None and found.kind is AlertKind.PRICE and found.level is None


@pytest.mark.parametrize(
    ("text", "edge"),
    [
        ("avise quando minha posição estiver a 3% da borda", "3"),
        ("warn me when my LP is within 5% of the range edge", "5"),
        ("me avisa quando meu LP estiver a 2,5% do limite da faixa", "2.5"),
        ("alert me when my uniswap position is 10% from the edge", "10"),
        ("notify me when my position gets near the edge", "5"),
    ],
)
def test_near_edge_requests_are_range_alerts_with_a_distance(text: str, edge: str) -> None:
    found = alert_intent(text)
    assert found is not None and found.kind is AlertKind.LP_RANGE
    assert found.edge_percent == Decimal(edge)


def test_a_plain_range_request_has_no_edge_distance() -> None:
    found = alert_intent("tell me when my LP goes out of range")
    assert found is not None and found.edge_percent is None


@pytest.mark.parametrize(
    "text",
    [
        # A price asked, not a watch: the market tools answer these.
        "what is the BTC price?",
        "quanto está o ETH?",
        "qual o preço do bitcoin hoje?",
        "tell me the ETH price",
        "tell me if BTC is above 100k",
        "me diga se o BTC está acima de 100k",
        # Questions about alerting.
        "how do I set an alert for BTC above 100k?",
        "can binance notify me when BTC goes above 100k?",
        "qual app consegue me avisar quando o BTC passar de 100k?",
        "does uniswap warn me when my LP is within 5% of the range edge?",
        # About the archive.
        "what did people say about BTC going above 100k?",
        "o que disseram sobre avisar quando o ETH cair abaixo de 2500?",
        # An alert verb, no asset.
        "me avisa quando o deploy passar de 100 testes",
        # History, not a watch, even after "tell me when".
        "tell me when BTC first went above 100k",
        "can you tell me when bitcoin crossed 100k?",
        "tell me if BTC ever hit 100k",
        "tell me when ETH was above 4000 in 2021",
        "tell me when ETH reached 4000",
        "tell me when BTC hit 100k",
        "me diga quando o BTC passou de 100k",
        "me diz quando o ETH chegou a 4 mil pela primeira vez",
        "me fala se o bitcoin alguma vez passar de 100k",
        # A number about a coin that is not its price in dollars.
        "remind me when the gas on ethereum drops below 20 gwei",
        "alert me when the ETH gas price goes over 50",
        "alert me when BTC dominance goes above 60%",
        # Ethereum the chain.
        "alert me when my pool on ethereum goes above 3000",
    ],
)
def test_price_and_edge_questions_are_not_requests(text: str) -> None:
    assert alert_intent(text) is None


@pytest.mark.parametrize(
    ("text", "asset", "direction", "level"),
    [
        ("tell me when ETH is above 4000", "ETH", ABOVE, "4000"),
        ("me diga quando o BTC passar de 100k", "BTC", ABOVE, "100000"),
        # The direction nearest the level, not the first word that could be one.
        ("alert me if ETH over the next week drops below 2500", "ETH", BELOW, "2500"),
        ("alert me when ETH goes over 3000", "ETH", ABOVE, "3000"),
        ("avisa quando o preço do ethereum passar de 4 mil", "ETH", ABOVE, "4000"),
    ],
)
def test_a_change_still_to_come_is_a_price_request(
    text: str, asset: str, direction: PriceDirection, level: str
) -> None:
    found = alert_intent(text)
    assert found == AlertIntent(
        AlertKind.PRICE, asset=asset, direction=direction, level=Decimal(level)
    )


def test_ethereum_the_chain_keeps_a_range_request_a_range_request() -> None:
    found = alert_intent("tell me when my LP on ethereum goes out of range")
    assert found is not None and found.kind is AlertKind.LP_RANGE


def test_price_requests_are_answered_in_portuguese() -> None:
    assert alert_language("avisa quando o BTC passar de 100k", None) is PT
    assert alert_language("alert me when ETH goes above $3,000", None) is EN


# --- reading a level ------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "value"),
    [
        ("2.500", "2500"),
        ("2,500", "2500"),
        ("3,000", "3000"),
        ("100.000", "100000"),
        ("1.250.000", "1250000"),
        ("1,234.56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("2500", "2500"),
        ("1.5", "1.5"),
        ("2,75", "2.75"),
    ],
)
def test_either_notation_reads_the_same(written: str, value: str) -> None:
    assert parse_amount(written) == Decimal(value)


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("alert me when BTC goes above 100k", "100000"),
        ("alert me when BTC goes above 100K", "100000"),
        ("alert me when ETH goes above 3k", "3000"),
        ("alert me when ETH goes above 1.5k", "1500"),
        ("alert me when ETH goes above $3,000", "3000"),
        ("me avisa quando o ETH passar de US$ 3.000", "3000"),
        ("me avisa quando o BTC passar de 100 mil", "100000"),
        ("alert me when ETH goes above 3000.", "3000"),
    ],
)
def test_the_level_is_read_in_dollars(text: str, level: str) -> None:
    found = alert_intent(text)
    assert found is not None and found.level == Decimal(level)


def test_a_percentage_is_not_a_level() -> None:
    found = alert_intent("alert me when BTC rises 5%")
    assert found is not None and found.level is None


def test_the_assets_are_the_market_tools_vocabulary() -> None:
    assert PRICE_ASSETS == CRYPTO_ASSETS


# --- the price crossing ---------------------------------------------------------


def price_alert(
    state: AlertState, direction: PriceDirection = ABOVE, level: str = "100000", **changes: Any
) -> PositionAlert:
    alert = PositionAlert(
        id=21,
        person=LEO,
        kind=AlertKind.PRICE,
        chain=None,
        address=None,
        address_source=None,
        language=PT,
        state=state,
        state_since=EARLIER,
        created_at=EARLIER,
        next_check_at=NOW,
        price=PriceTarget("BTC", direction, Decimal(level)),
    )
    return replace(alert, **changes)


def btc(price: str) -> PriceObservation:
    return PriceObservation("BTC", Decimal(price), NOW - timedelta(minutes=1), "CoinGecko")


def test_crossing_up_fires_once() -> None:
    fired = evaluate(price_alert(AlertState.BELOW), btc("100412.35"), NOW)

    assert fired.firing is not None and fired.firing.kind is FiringKind.PRICE_CROSSED
    assert fired.update.state is AlertState.ABOVE and fired.update.fired
    assert fired.update.last_value == Decimal("100412.35")

    again = evaluate(price_alert(AlertState.ABOVE), btc("101000"), NOW)
    assert again.firing is None and again.update.state is AlertState.ABOVE


def test_reaching_the_level_exactly_counts() -> None:
    assert evaluate(price_alert(AlertState.BELOW), btc("100000"), NOW).firing is not None


@pytest.mark.parametrize("price", ["99999", "99600", "99500.01"])
def test_a_fired_alert_holds_inside_the_band(price: str) -> None:
    held = evaluate(price_alert(AlertState.ABOVE), btc(price), NOW)
    assert held.firing is None and held.update.state is AlertState.ABOVE


def test_crossing_back_past_the_band_rearms_silently_and_the_next_crossing_fires() -> None:
    rearmed = evaluate(price_alert(AlertState.ABOVE), btc("99500"), NOW)
    assert rearmed.firing is None and rearmed.update.state is AlertState.BELOW

    again = evaluate(price_alert(AlertState.BELOW), btc("100001"), NOW)
    assert again.firing is not None


def test_a_below_alert_fires_going_down_and_rearms_above_the_band() -> None:
    down = price_alert(AlertState.ABOVE, BELOW, "2500")
    fired = evaluate(down, btc("2499"), NOW)
    assert fired.firing is not None and fired.update.state is AlertState.BELOW

    below = price_alert(AlertState.BELOW, BELOW, "2500")
    assert evaluate(below, btc("2510"), NOW).update.state is AlertState.BELOW
    rearmed = evaluate(below, btc("2512.50"), NOW)
    assert rearmed.firing is None and rearmed.update.state is AlertState.ABOVE


def test_a_price_for_another_asset_is_a_failure() -> None:
    eth = PriceObservation("ETH", Decimal("3000"), NOW, "CoinGecko")
    result = evaluate(price_alert(AlertState.BELOW), eth, NOW)
    assert result.firing is None and result.update.failed


def test_the_price_message_has_the_price_and_its_time_in_the_alerts_language() -> None:
    [pt] = [evaluate(price_alert(AlertState.BELOW), btc("100412.35"), NOW).firing]
    assert pt is not None
    assert render_alert(pt).startswith(
        "🔔 **Alerta** — o BTC **passou de US$ 100.000**: agora US$ 100.412,35 "
        "(CoinGecko, 2026-09-01 11:59 UTC)."
    )
    down = price_alert(AlertState.ABOVE, BELOW, "2500", language=EN)
    en = evaluate(down, btc("2480.5"), NOW).firing
    assert en is not None
    assert "BTC fell **below $2,500**: now $2,480.50 (CoinGecko, 2026-09-01 11:59 UTC)." in (
        render_alert(en)
    )


# --- near the edge --------------------------------------------------------------


def test_the_distance_on_the_live_arbitrum_position() -> None:
    """#210171 at tick -197404 in [-197920, -197070): 334 ticks under the top."""
    reading = arbitrum()
    near = edge_distance(reading.price, reading.price_lower, reading.price_upper)

    assert near.edge is Edge.UPPER
    assert Decimal("3.39") < near.percent < Decimal("3.40")
    # The same move as the ticks give: 1.0001 ** 334 - 1.
    assert abs(near.percent - (Decimal("1.0001") ** 334 - 1) * 100) < Decimal("0.0001")


def test_the_nearer_edge_can_be_the_lower_one() -> None:
    near = edge_distance(Decimal(95), Decimal(90), Decimal(200))
    assert near.edge is Edge.LOWER
    assert near.percent == (1 - Decimal(90) / Decimal(95)) * 100


def edge_alert(state: AlertState, edge: str = "5", **changes: Any) -> PositionAlert:
    alert = PositionAlert(
        id=31,
        person=LEO,
        kind=AlertKind.LP_RANGE,
        chain="arbitrum",
        address=WALLET,
        language=EN,
        state=state,
        state_since=EARLIER,
        created_at=EARLIER,
        next_check_at=NOW,
        lp=V4,
        edge_percent=Decimal(edge),
    )
    return replace(alert, **changes)


def test_in_range_to_near_the_edge_fires_once_after_confirmation() -> None:
    pending = evaluate(edge_alert(AlertState.IN_RANGE), arbitrum(), NOW)
    assert pending.firing is None
    assert pending.update.pending_state is AlertState.NEAR_EDGE

    confirmed = edge_alert(
        AlertState.IN_RANGE, pending_state=AlertState.NEAR_EDGE, pending_count=1
    )
    fired = evaluate(confirmed, arbitrum(), NOW)
    assert fired.firing is not None and fired.firing.kind is FiringKind.NEAR_EDGE
    assert fired.update.state is AlertState.NEAR_EDGE

    still = evaluate(edge_alert(AlertState.NEAR_EDGE), arbitrum(), NOW)
    assert still.firing is None and still.update.pending_state is None


def test_outside_the_distance_it_is_in_range() -> None:
    """3.4% from the top is not within 3%."""
    result = evaluate(edge_alert(AlertState.IN_RANGE, edge="3"), arbitrum(), NOW)
    assert result.update.pending_state is None and result.update.state is AlertState.IN_RANGE


def test_a_fired_warning_rearms_only_a_point_further_away_and_silently() -> None:
    # 3.4% from the edge: out of a 3% warning, but inside its one-point band.
    held = evaluate(edge_alert(AlertState.NEAR_EDGE, edge="3"), arbitrum(), NOW)
    assert held.update.state is AlertState.NEAR_EDGE and held.update.pending_state is None

    farther = edge_alert(
        AlertState.NEAR_EDGE,
        edge="2",
        pending_state=AlertState.IN_RANGE,
        pending_count=1,
    )
    rearmed = evaluate(farther, arbitrum(), NOW)
    assert rearmed.firing is None and rearmed.update.state is AlertState.IN_RANGE


def test_leaving_the_range_from_near_the_edge_and_coming_back_are_told() -> None:
    out = edge_alert(
        AlertState.NEAR_EDGE, pending_state=AlertState.OUT_OF_RANGE, pending_count=1
    )
    left = evaluate(out, arbitrum(-197000), NOW)
    assert left.firing is not None and left.firing.kind is FiringKind.OUT_OF_RANGE

    back = edge_alert(
        AlertState.OUT_OF_RANGE, pending_state=AlertState.NEAR_EDGE, pending_count=1
    )
    returned = evaluate(back, arbitrum(), NOW)
    assert returned.firing is not None and returned.firing.kind is FiringKind.BACK_IN_RANGE
    assert returned.update.state is AlertState.NEAR_EDGE


def test_the_near_edge_message_names_the_edge_and_the_distance() -> None:
    confirmed = edge_alert(
        AlertState.IN_RANGE, pending_state=AlertState.NEAR_EDGE, pending_count=1
    )
    firing = evaluate(confirmed, arbitrum(), NOW).firing
    assert firing is not None
    assert "#210171 on Arbitrum is **3.4% from the upper edge** of its range" in (
        render_alert(firing)
    )
    pt = evaluate(replace(confirmed, language=PT), arbitrum(), NOW).firing
    assert pt is not None
    assert "na Arbitrum está **a 3,4% da borda superior** da faixa" in render_alert(pt)


# --- creating -------------------------------------------------------------------


def new_price(**changes: Any) -> NewAlert:
    alert = NewAlert(
        person=LEO,
        kind=AlertKind.PRICE,
        chain=None,
        address=None,
        address_source=None,
        language=EN,
        price=PriceTarget("BTC", ABOVE, Decimal(100000)),
        state=AlertState.BELOW,
    )
    return replace(alert, **changes)


async def test_a_price_alert_is_created_with_no_wallet() -> None:
    store = MemoryStore()
    assert (await AlertService(store).create(new_price(), NOW)).created
    [(stored, _)] = store.created
    assert stored.address is None and stored.chain is None


@pytest.mark.parametrize(
    "alert",
    [
        new_price(price=PriceTarget("SOL", ABOVE, Decimal(200))),
        new_price(price=PriceTarget("BTC", ABOVE, Decimal(0))),
        new_price(price=None),
        new_price(address=WALLET),
        new_price(chain="base"),
        new_price(threshold=Decimal("1.3")),
    ],
    ids=["asset", "level", "no-target", "address", "chain", "threshold"],
)
async def test_a_price_alert_with_anything_else_is_refused(alert: NewAlert) -> None:
    result = await AlertService(MemoryStore()).create(alert, NOW)
    assert result.refusal is AlertRefusal.INVALID_TARGET


@pytest.mark.parametrize(("edge", "ok"), [("0.5", False), ("1", True), ("50", True), ("51", False)])
async def test_the_edge_distance_is_bounded(edge: str, ok: bool) -> None:
    alert = NewAlert(
        person=LEO,
        kind=AlertKind.LP_RANGE,
        chain="arbitrum",
        address=WALLET,
        address_source=None,
        language=EN,
        lp=V4,
        edge_percent=Decimal(edge),
    )
    result = await AlertService(MemoryStore()).create(alert, NOW)
    assert result.created is ok
    if not ok:
        assert result.refusal is AlertRefusal.EDGE_OUT_OF_RANGE


class Prices:
    """`PriceFeed`, scripted."""

    def __init__(self, found: Mapping[str, PriceObservation] | Exception) -> None:
        self.found = found
        self.calls = 0

    async def latest(self) -> Mapping[str, PriceObservation]:
        self.calls += 1
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


NOTHING = TargetsRead()


def flow(
    prices: Prices | None, found: TargetsRead = NOTHING, store: Store | None = None
) -> tuple[AlertRequests, Store]:
    store = store or Store()
    service = AlertService(store)  # type: ignore[arg-type]
    return AlertRequests(service, Chain(found), prices=prices, clock=lambda: NOW), store


async def price_proposal(
    intent: AlertIntent, prices: Prices | None, language: AlertLanguage = EN
) -> Any:
    requests, _ = flow(prices)
    return await requests.propose(
        LEO, intent, saved_wallets=(), language=language, direct=False
    )


def price_intent(direction: PriceDirection | None, level: str = "100000") -> AlertIntent:
    return AlertIntent(AlertKind.PRICE, asset="BTC", direction=direction, level=Decimal(level))


async def test_a_price_proposal_shows_the_price_now_and_needs_no_wallet() -> None:
    prices = Prices({"BTC": btc("97412.35")})
    reply = await price_proposal(price_intent(ABOVE), prices, PT)

    assert prices.calls == 1
    assert "• **BTC** acima de **US$ 100.000** · agora **US$ 97.412,35** (CoinGecko," in (
        reply.text
    )
    assert "Confirmar" in reply.text
    [alert] = reply.proposal.alerts
    assert alert.address is None and alert.chain is None and alert.address_source is None
    assert alert.state is AlertState.BELOW
    assert alert.last_value == Decimal("97412.35")


@pytest.mark.parametrize(("level", "direction"), [("100000", ABOVE), ("90000", BELOW)])
async def test_a_level_with_no_side_is_the_side_the_price_is_not_on(
    level: str, direction: PriceDirection
) -> None:
    reply = await price_proposal(price_intent(None, level), Prices({"BTC": btc("97412.35")}))
    [alert] = reply.proposal.alerts
    assert alert.price.direction is direction


async def test_a_level_already_passed_says_so() -> None:
    reply = await price_proposal(price_intent(ABOVE, "90000"), Prices({"BTC": btc("97412.35")}))
    assert ", already there: I'll message you the next time it crosses" in reply.text
    [alert] = reply.proposal.alerts
    assert alert.state is AlertState.ABOVE


async def test_no_level_asks_for_one_and_reads_nothing() -> None:
    prices = Prices({"BTC": btc("97412.35")})
    reply = await price_proposal(AlertIntent(AlertKind.PRICE, asset="BTC"), prices)
    assert reply.proposal is None and reply.text.startswith("At what price?")
    assert prices.calls == 0


async def test_an_unreadable_price_is_said_and_nothing_offered() -> None:
    reply = await price_proposal(price_intent(ABOVE), Prices(ProviderUnavailable("down")), PT)
    assert reply.proposal is None
    assert reply.text.startswith("Não consegui ler o preço do BTC agora")


async def test_without_a_price_source_price_alerts_are_unavailable() -> None:
    reply = await price_proposal(price_intent(ABOVE), None)
    assert reply.proposal is None and reply.text.startswith("Price alerts aren't available")


async def test_a_confirmed_price_alert_is_listed_with_its_level() -> None:
    requests, store = flow(Prices({"BTC": btc("97412.35")}))
    reply = await requests.propose(
        LEO, price_intent(ABOVE), saved_wallets=(), language=EN, direct=True
    )
    done = await requests.confirm(reply.proposal)

    assert "• **1** — BTC above $100,000" in done
    again = await requests.propose(
        LEO, price_intent(ABOVE), saved_wallets=(), language=EN, direct=True
    )
    assert again.proposal is None and again.text.startswith("I'm already watching")
    listing = alert_listing(store.rows, EN)
    assert "**1** - BTC above $100,000 - below the level, now $97,412.35 since" in listing


def arbitrum_candidate() -> LpCandidate:
    reading = arbitrum()
    return LpCandidate(
        chain="arbitrum",
        target=V4,
        state=reading.state,
        tick=TICK,
        price=reading.price,
        price_lower=reading.price_lower,
        price_upper=reading.price_upper,
        base_symbol=reading.base_symbol,
        quote_symbol=reading.quote_symbol,
    )


async def edge_proposal(edge: str, language: AlertLanguage = EN, store: Store | None = None) -> Any:
    requests, _ = flow(None, TargetsRead(lp=(arbitrum_candidate(),)), store)
    intent = AlertIntent(AlertKind.LP_RANGE, edge_percent=Decimal(edge))
    return await requests.propose(
        LEO, intent, saved_wallets=(SAVED,), language=language, direct=True
    )


async def test_the_edge_confirmation_shows_the_distance_to_the_nearer_edge() -> None:
    reply = await edge_proposal("3")

    assert "#210171" in reply.text and "**Arbitrum**" in reply.text
    assert "**3.4% from the upper edge**; I'll warn you within 3%" in reply.text
    [alert] = reply.proposal.alerts
    assert alert.edge_percent == Decimal(3) and alert.state is AlertState.IN_RANGE

    pt = await edge_proposal("5", PT)
    assert "**a 3,4% da borda superior**; te aviso a 5%" in pt.text
    assert "já está perto assim" in pt.text
    [near] = pt.proposal.alerts
    assert near.state is AlertState.NEAR_EDGE


@pytest.mark.parametrize("edge", ["0.5", "60"])
async def test_an_edge_distance_out_of_bounds_is_refused_with_the_bounds(edge: str) -> None:
    reply = await edge_proposal(edge)
    assert reply.proposal is None
    assert reply.text.startswith("I can warn you between 1% and 50% from a range edge")


async def test_an_edge_warning_is_offered_on_a_position_already_watched() -> None:
    """The store adds the distance to the existing alert; offering it is this side's part."""
    store = Store()
    plain = NewAlert(
        person=LEO,
        kind=AlertKind.LP_RANGE,
        chain="arbitrum",
        address=SAVED,
        address_source=None,
        language=EN,
        lp=V4,
        state=AlertState.IN_RANGE,
    )
    await store.create(plain, NOW)

    reply = await edge_proposal("3", store=store)

    assert reply.proposal is not None
    listing = alert_listing(store.rows, EN)
    assert "#210171 on Arbitrum - in range" in listing


def watched_arbitrum(**changes: Any) -> NewAlert:
    alert = NewAlert(
        person=LEO,
        kind=AlertKind.LP_RANGE,
        chain="arbitrum",
        address=SAVED,
        address_source=None,
        language=EN,
        lp=V4,
        state=AlertState.IN_RANGE,
    )
    return replace(alert, **changes)


async def test_a_plain_range_request_is_covered_by_an_edge_warning() -> None:
    store = Store()
    await store.create(watched_arbitrum(edge_percent=Decimal(5)), NOW)
    requests, _ = flow(None, TargetsRead(lp=(arbitrum_candidate(),)), store)

    reply = await requests.propose(
        LEO, AlertIntent(AlertKind.LP_RANGE), saved_wallets=(SAVED,), language=EN, direct=True
    )

    assert reply.proposal is None and reply.text.startswith("I'm already watching")


async def test_an_edge_warning_on_a_watched_position_is_offered_at_the_cap() -> None:
    """It adds to an alert rather than taking a slot; a new position still needs one."""
    store = Store(cap=1)
    await store.create(watched_arbitrum(), NOW)

    def at_cap(candidate: LpCandidate) -> AlertRequests:
        service = AlertService(store)  # type: ignore[arg-type]
        return AlertRequests(service, Chain(TargetsRead(lp=(candidate,))), cap=1)

    edge = AlertIntent(AlertKind.LP_RANGE, edge_percent=Decimal(3))
    reply = await at_cap(arbitrum_candidate()).propose(
        LEO, edge, saved_wallets=(SAVED,), language=EN, direct=True
    )
    assert reply.proposal is not None
    [alert] = reply.proposal.alerts
    assert alert.edge_percent == Decimal(3) and alert.lp == V4

    other = replace(arbitrum_candidate(), target=replace(V4, token_id=999))
    refused = await at_cap(other).propose(
        LEO, edge, saved_wallets=(SAVED,), language=EN, direct=True
    )
    assert refused.proposal is None and refused.text.startswith("You already have 1 alerts")


# --- reading prices ---------------------------------------------------------------


def coingecko(payloads: list[dict[str, Any]]) -> tuple[CoinGeckoProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payloads[min(len(seen), len(payloads)) - 1])

    provider = CoinGeckoProvider(
        CallBudget(), limiter=RateLimiter(0), transport=httpx.MockTransport(handle)
    )
    return provider, seen


QUOTES = {
    "bitcoin": {"usd": 100412.35, "last_updated_at": 1788264000},
    "ethereum": {"usd": 2480.5, "last_updated_at": 1788264000},
}


async def test_one_constant_request_prices_every_asset_and_is_cached() -> None:
    provider, seen = coingecko([QUOTES])
    prices = AlertPrices(provider)

    first = await prices.latest()
    second = await prices.latest()

    assert len(seen) == 1, "a fresh cache answers the second read"
    assert first == second
    assert first["BTC"].price == Decimal("100412.35")
    assert first["ETH"].source == "CoinGecko"
    assert first["BTC"].as_of == datetime.fromtimestamp(1788264000, UTC)
    [request] = seen
    assert request.url.host == "api.coingecko.com"
    assert request.url.params["ids"] == "bitcoin,ethereum"


async def test_every_price_alert_in_a_sweep_is_one_request() -> None:
    provider, seen = coingecko([QUOTES])
    eth = price_alert(AlertState.ABOVE, BELOW, "2500", id=22)
    eth = replace(eth, price=PriceTarget("ETH", BELOW, Decimal(2500)))

    readings = await AlertPrices(provider).observe([price_alert(AlertState.BELOW), eth])

    assert len(seen) == 1
    assert readings[21] == PriceObservation(
        "BTC", Decimal("100412.35"), datetime.fromtimestamp(1788264000, UTC), "CoinGecko"
    )
    assert isinstance(readings[22], PriceObservation) and readings[22].asset == "ETH"


async def test_no_price_is_a_failed_read_for_every_price_alert() -> None:
    provider, _ = coingecko([{}])
    readings = await AlertPrices(provider).observe([price_alert(AlertState.BELOW)])
    assert isinstance(readings[21], ReadFailure)


async def test_a_coin_missing_from_the_response_leaves_the_other_priced() -> None:
    provider, seen = coingecko([{"ethereum": QUOTES["ethereum"]}])
    eth = replace(
        price_alert(AlertState.ABOVE, id=22), price=PriceTarget("ETH", BELOW, Decimal(2500))
    )

    readings = await AlertPrices(provider).observe([price_alert(AlertState.BELOW), eth])

    assert len(seen) == 1
    assert isinstance(readings[21], ReadFailure)
    assert isinstance(readings[22], PriceObservation) and readings[22].price == Decimal("2480.5")


async def test_the_sweep_reads_prices_apart_from_the_chain() -> None:
    priced = price_alert(AlertState.BELOW)
    held = edge_alert(AlertState.IN_RANGE)
    chain = FakeObserver({held.id: arbitrum()})
    prices = FakeObserver({priced.id: btc("100412.35")})
    messenger = FakeMessenger()

    sent = await AlertRunner(MemoryStore([priced, held]), chain, messenger, prices=prices).run_due(
        NOW
    )

    assert sent == 1
    assert chain.seen == [[held]], "a price alert never reaches the chain watcher"
    assert prices.seen == [[priced]]
    [(_, alert_id, text)] = messenger.sent
    assert alert_id == priced.id and "passou de US$ 100.000" in text


async def test_without_a_price_reader_a_price_alert_is_a_failed_read() -> None:
    store = MemoryStore([price_alert(AlertState.BELOW)])
    chain = FakeObserver({})

    assert await AlertRunner(store, chain, FakeMessenger()).run_due(NOW) == 0
    assert chain.seen == []
    [(_, update)] = store.records
    assert update.failed
