"""The preferred currency: how it is said, stored, and shown beside dollars.

The end-to-end scenarios (tests/e2e/test_preferred_currency.py) run the whole
process. These pin down the parts that need no network: which words name
which currency, what is refused, how a figure is written in each language,
and that a rate that cannot be read leaves an answer in dollars alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from chatmemory.adapters.chain.positions_render import dollars, usd
from chatmemory.adapters.market.coingecko import CoinGeckoProvider
from chatmemory.adapters.market.frankfurter import FrankfurterProvider
from chatmemory.adapters.market.quotes import Quote, Timing
from chatmemory.adapters.market.usd_rates import UsdReferenceRates
from chatmemory.adapters.web.limits import CallBudget
from chatmemory.app.alerts import evaluate, render_alert
from chatmemory.app.asker import AskerFacts, answering_with_facts
from chatmemory.app.currency import (
    Conversion,
    PreferredCurrencies,
    asker_conversion,
    beside,
    conversion_to,
    money,
    rate_note,
)
from chatmemory.app.egress import ISO_4217_CODES
from chatmemory.app.fact_replies import fact_set_reply, facts_shown_reply
from chatmemory.app.facts import FactOutcome, PersonalFactsService, visible_facts
from chatmemory.app.language import Language
from chatmemory.app.routing import FactAction, fact_intent
from chatmemory.domain.currency import SUPPORTED_CODES, currency_code
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.alerts import AlertState
from chatmemory.ports.facts import FactKind, FactRejection, InvalidFact, PersonalFact
from chatmemory.ports.memory import ConversationLocation
from tests.unit.test_alert_kinds import NOW, btc, price_alert
from tests.unit.test_facts import FakeFactStore

EN, PT = Language.ENGLISH, Language.PORTUGUESE
LEO = PersonRef("discord", 7)
IN_CHANNEL = ConversationLocation("discord", 10, direct=False)
BRL_RATE = Decimal("5.46")
REAIS = Conversion("BRL", BRL_RATE, PT)


class FixedRates:
    """`UsdRates` answering one rate, or None, and counting the asks."""

    def __init__(self, rate: Decimal | None = BRL_RATE) -> None:
        self.rate = rate
        self.asked: list[str] = []

    async def usd_to(self, code: str) -> Decimal | None:
        self.asked.append(code)
        return self.rate


# --- the vocabulary ------------------------------------------------------------


def test_every_supported_code_is_an_iso_code_the_fx_tool_accepts() -> None:
    assert SUPPORTED_CODES <= ISO_4217_CODES


@pytest.mark.parametrize(
    ("said", "code"),
    [
        ("reais", "BRL"),
        ("o real brasileiro", "BRL"),
        ("Real Brasileiro", "BRL"),
        ("BRL", "BRL"),
        ("brl", "BRL"),
        ("euros", "EUR"),
        ("o euro", "EUR"),
        ("libra", "GBP"),
        ("pound sterling", "GBP"),
        ("iene", "JPY"),
        ("yen", "JPY"),
        ("dólares", "USD"),
        ("peso mexicano", "MXN"),
        ("franco suíço", "CHF"),
    ],
)
def test_a_currency_is_read_from_its_name_in_either_language(said: str, code: str) -> None:
    assert currency_code(said) == code


@pytest.mark.parametrize(
    "said", ["peso argentino", "dogecoin", "bitcoin", "ARS", "reais e sou dev"]
)
def test_a_currency_the_source_does_not_publish_is_not_a_code(said: str) -> None:
    assert currency_code(said) is None


# --- recognising the statement -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("prefiro ver em reais", "reais"),
        ("minha moeda é o real brasileiro", "o real brasileiro"),
        ("my currency is euro", "euro"),
        ("moeda preferida: BRL", "BRL"),
        ("I prefer to see prices in euros", "euros"),
        ("change my currency to yen", "yen"),
        ("minha moeda preferida é euro, por favor", "euro"),
    ],
)
def test_a_preferred_currency_is_recognised(text: str, value: str) -> None:
    intent = fact_intent(text)
    assert intent is not None and intent.action is FactAction.SET
    assert (intent.kind, intent.value) == (FactKind.PREFERRED_CURRENCY, value)


def test_a_currency_inside_an_introduction_is_one_of_its_facts() -> None:
    intent = fact_intent("Oi, me chamo Leo, moro no Brasil e uso reais")
    assert intent is not None and intent.action is FactAction.SET_MANY
    assert (FactKind.PREFERRED_CURRENCY, "reais") in intent.sets
    assert (FactKind.PREFERRED_NAME, "Leo") in intent.sets


@pytest.mark.parametrize(
    "text",
    ["uso o discord", "prefiro ver em português", "quanto é 100 dólares em reais?",
     "qual o preço do bitcoin?"],
)
def test_words_near_a_currency_are_not_a_preference(text: str) -> None:
    intent = fact_intent(text)
    assert intent is None or intent.kind is not FactKind.PREFERRED_CURRENCY


@pytest.mark.parametrize(
    ("text", "action"),
    [("esqueça minha moeda", FactAction.FORGET), ("forget my currency", FactAction.FORGET),
     ("qual a minha moeda?", FactAction.SHOW), ("what's my currency?", FactAction.SHOW)],
)
def test_the_currency_can_be_asked_for_and_forgotten(text: str, action: FactAction) -> None:
    intent = fact_intent(text)
    assert intent is not None
    assert (intent.action, intent.kind) == (action, FactKind.PREFERRED_CURRENCY)


# --- storing it ----------------------------------------------------------------


def test_the_code_is_what_is_stored() -> None:
    assert PersonalFact(FactKind.PREFERRED_CURRENCY, "o real brasileiro").value == "BRL"


async def test_an_unsupported_currency_is_refused_with_the_supported_list() -> None:
    store = FakeFactStore()
    facts = PersonalFactsService(store)
    result = await facts.remember(
        Viewer(LEO, frozenset()), FactKind.PREFERRED_CURRENCY, "peso argentino"
    )

    assert result.outcome is FactOutcome.REJECTED
    assert result.rejection is FactRejection.MALFORMED
    assert not store.rows
    reply = fact_set_reply(result, direct=True, language=PT)
    assert reply.startswith("Não salvei isso como sua moeda preferida")
    assert "BRL" in reply and "EUR" in reply and "USD" in reply
    assert "peso argentino" not in reply


def test_dogecoin_is_malformed() -> None:
    with pytest.raises(InvalidFact) as refused:
        PersonalFact(FactKind.PREFERRED_CURRENCY, "dogecoin")
    assert refused.value.reason is FactRejection.MALFORMED


async def test_the_confirmation_names_the_currency_in_the_askers_language() -> None:
    facts = PersonalFactsService(FakeFactStore())
    result = await facts.remember(Viewer(LEO, frozenset()), FactKind.PREFERRED_CURRENCY, "reais")
    assert fact_set_reply(result, direct=False, language=PT) == (
        "Certo, vou mostrar os valores em dólar e também em **real brasileiro (BRL)**."
    )
    dollars_only = await facts.remember(
        Viewer(LEO, frozenset()), FactKind.PREFERRED_CURRENCY, "dollars"
    )
    assert fact_set_reply(dollars_only, direct=False, language=EN) == (
        "Got it, I'll show amounts in US dollars only."
    )


async def test_the_currency_is_shown_in_a_channel_like_the_language() -> None:
    store = FakeFactStore()
    await store.set_fact(LEO, PersonalFact(FactKind.PREFERRED_CURRENCY, "euros"))
    facts = visible_facts(await store.facts_of(Viewer(LEO, frozenset())), IN_CHANNEL)
    assert facts.get(FactKind.PREFERRED_CURRENCY) == "EUR"
    assert "Moeda preferida: **euro (EUR)**" in facts_shown_reply(facts, False, PT)


# --- writing a figure ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "code", "language", "shown"),
    [
        ("345120", "BRL", PT, "R$ 345.120,00"),
        ("345120", "BRL", EN, "R$345,120.00"),
        ("1234.5", "EUR", PT, "€ 1.234,50"),
        ("1234.567", "JPY", EN, "¥1,235"),
        ("1234.5", "CHF", EN, "CHF 1,234.50"),
        ("1234.5", "CHF", PT, "CHF 1.234,50"),
    ],
)
def test_a_figure_is_written_as_its_reader_writes_it(
    value: str, code: str, language: Language, shown: str
) -> None:
    assert money(Decimal(value), code, language) == shown


def test_a_dollar_figure_gets_the_converted_one_beside_it() -> None:
    assert usd(Decimal("100"), REAIS) == " ≈ $100.00 (R$ 546,00)"
    assert dollars(Decimal("100"), REAIS) == "$100.00 (R$ 546,00)"
    assert usd(Decimal("100")) == " ≈ $100.00"
    assert beside(None, Decimal("100")) == ""
    assert rate_note(REAIS) == (
        "_BRL pela taxa de referência diária: 1 USD = 5,4600 BRL (Frankfurter/BCE)._"
    )


# --- the conversion ------------------------------------------------------------


async def test_dollars_as_the_preference_add_nothing_and_read_no_rate() -> None:
    rates = FixedRates()
    assert await conversion_to("USD", rates, EN) is None
    assert await conversion_to(None, rates, EN) is None
    assert rates.asked == []


async def test_no_rate_means_no_second_figure() -> None:
    assert await conversion_to("BRL", FixedRates(None), PT) is None


async def test_the_askers_preference_comes_from_their_facts_for_this_answer() -> None:
    rates = FixedRates()
    assert await asker_conversion(rates, "qual o preço do bitcoin?") is None
    with answering_with_facts(AskerFacts(LEO, preferred_currency="BRL")):
        conversion = await asker_conversion(rates, "qual o preço do bitcoin?")
    assert conversion == Conversion("BRL", BRL_RATE, PT)
    assert rates.asked == ["BRL"]


async def test_an_alert_owner_is_priced_from_their_stored_preference() -> None:
    store = FakeFactStore()
    await store.set_fact(LEO, PersonalFact(FactKind.PREFERRED_CURRENCY, "BRL"))
    currencies = PreferredCurrencies(store, FixedRates())
    assert await currencies.conversion_for(LEO, PT) == REAIS
    assert await currencies.conversion_for(PersonRef("discord", 8), PT) is None


def test_a_price_alert_shows_the_price_in_the_preferred_currency_too() -> None:
    firing = evaluate(price_alert(AlertState.BELOW), btc("100000"), NOW).firing
    assert firing is not None
    text = render_alert(firing, conversion=REAIS)
    assert "agora US$ 100.000,00 (R$ 546.000,00)" in text
    assert "passou de US$ 100.000**" in text, "the level stays as it was asked"


# --- the rate --------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _frankfurter(handle: httpx.MockTransport) -> FrankfurterProvider:
    return FrankfurterProvider(CallBudget(4), ttl_seconds=0, transport=handle)


def _rates_answer(requests: list[httpx.Request], status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(
            200, json={"base": "USD", "date": "2026-09-01", "rates": {"BRL": 5.46}}
        )

    return httpx.MockTransport(handle)


async def test_one_rate_is_read_and_then_served_from_the_cache() -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    rates = UsdReferenceRates(_frankfurter(_rates_answer(requests)), clock=clock)

    assert await rates.usd_to("BRL") == BRL_RATE
    assert await rates.usd_to("BRL") == BRL_RATE
    assert len(requests) == 1
    [sent] = requests
    assert sent.url.host == "api.frankfurter.dev"
    assert dict(sent.url.params) == {"from": "USD", "to": "BRL"}, "two codes, no amount"

    clock.now += 601
    assert await rates.usd_to("BRL") == BRL_RATE
    assert len(requests) == 2, "an expired rate is read again, never reused"


async def test_a_failing_rate_source_is_no_rate_and_no_exception() -> None:
    requests: list[httpx.Request] = []
    rates = UsdReferenceRates(_frankfurter(_rates_answer(requests, status=503)))
    assert await rates.usd_to("BRL") is None


async def test_only_a_supported_code_ever_leaves() -> None:
    requests: list[httpx.Request] = []
    rates = UsdReferenceRates(_frankfurter(_rates_answer(requests)))
    assert await rates.usd_to("ARS") is None
    assert await rates.usd_to("USD") is None
    assert requests == []


# --- the market tool's figure ------------------------------------------------------


def _quote() -> Quote:
    return Quote(
        instrument="Bitcoin (BTC)",
        value=Decimal("63210"),
        unit="USD",
        source="CoinGecko",
        url="https://www.coingecko.com/en/coins/bitcoin",
        timing=Timing.QUOTE_TIME,
        as_of=datetime(2026, 9, 1, 12, tzinfo=UTC) - timedelta(minutes=1),
    )


async def test_the_crypto_price_carries_the_converted_figure_itself() -> None:
    provider = CoinGeckoProvider(CallBudget(4), rates=FixedRates())
    line = "Bitcoin (BTC): 63,210 USD"
    with answering_with_facts(AskerFacts(LEO, preferred_currency="BRL")):
        annotated = await provider.annotate(line, _quote(), "qual o preço do bitcoin?")
    assert annotated.startswith("Bitcoin (BTC): 63,210 USD (R$ 345.126,60)\n")
    assert annotated.endswith(
        "\n_BRL pela taxa de referência diária: 1 USD = 5,4600 BRL (Frankfurter/BCE)._"
    )


async def test_without_a_rate_the_crypto_price_is_in_dollars_only() -> None:
    provider = CoinGeckoProvider(CallBudget(4), rates=FixedRates(None))
    line = "Bitcoin (BTC): 63,210 USD"
    with answering_with_facts(AskerFacts(LEO, preferred_currency="BRL")):
        assert await provider.annotate(line, _quote(), "qual o preço do bitcoin?") == line
