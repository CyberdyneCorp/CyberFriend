"""The preferred currency: how it is said, stored, and shown beside dollars.

The end-to-end scenarios (tests/e2e/test_preferred_currency.py) run the whole
process. These pin down the parts that need no network: which words name
which currency, what is refused, how a figure is written in each language,
and that a rate that cannot be read leaves an answer in dollars alone.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from chatmemory.adapters.chain.activity import ChainActivity, Recognised
from chatmemory.adapters.chain.activity_reader import ActivityReader
from chatmemory.adapters.chain.activity_render import render_activity
from chatmemory.adapters.chain.clearance import Cleared
from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.chain.portfolio import (
    ChainPortfolio,
    ChainWallet,
    Held,
    WalletPortfolio,
)
from chatmemory.adapters.chain.portfolio_reader import PortfolioReader
from chatmemory.adapters.chain.positions import ChainLending, ChainLiquidity
from chatmemory.adapters.chain.positions_provider import LIQUIDITY_TOOL, PositionsProvider
from chatmemory.adapters.chain.positions_render import (
    dollars,
    render_lending,
    render_liquidity,
    usd,
)
from chatmemory.adapters.chain.provider import WALLET_TOOL, PricedAsset, WalletProvider
from chatmemory.adapters.chain.rpc import ChainReader
from chatmemory.adapters.chain.tokens import BASE, ETHEREUM
from chatmemory.adapters.chain.uniswap import UniswapReader
from chatmemory.adapters.market.coingecko import CoinGeckoProvider
from chatmemory.adapters.market.frankfurter import FrankfurterProvider
from chatmemory.adapters.market.quotes import Quote, Timing
from chatmemory.adapters.market.usd_rates import UsdReferenceRates
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.app.alerts import AlertRunner, evaluate, render_alert
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
from chatmemory.app.egress import (
    ISO_4217_CODES,
    AuthorizedQuery,
    EgressGuard,
    EgressRequest,
    ProvenancedQuery,
    QueryOrigin,
    authorized,
)
from chatmemory.app.fact_replies import fact_set_reply, facts_shown_reply
from chatmemory.app.facts import FactOutcome, PersonalFactsService, visible_facts
from chatmemory.app.language import Language
from chatmemory.app.routing import FactAction, fact_intent
from chatmemory.app.wallet_activity import ActivityWindow
from chatmemory.composition import build_alert_runner
from chatmemory.domain.currency import SUPPORTED_CODES, currency_code
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.alerts import AlertState
from chatmemory.ports.facts import FactKind, FactRejection, InvalidFact, PersonalFact
from chatmemory.ports.memory import ConversationLocation
from tests.unit.activity_rows import WALLET, base_week
from tests.unit.test_alert_kinds import NOW, btc, price_alert
from tests.unit.test_defi_positions import OWNER
from tests.unit.test_defi_positions import _asset as lending_asset
from tests.unit.test_defi_positions import _position as lp_position
from tests.unit.test_facts import FakeFactStore
from tests.unit.test_position_alerts import (
    FakeMessenger,
    FakeObserver,
    MemoryStore,
    health_alert,
    hf,
)
from tests.unit.test_position_alerts_wiring import _Messenger as AlertMessenger
from tests.unit.test_position_alerts_wiring import settings as alert_settings
from tests.unit.test_wallet_activity import BASE_KNOWN, WINDOW
from tests.unit.test_wallet_activity import _chains as activity_chains
from tests.unit.test_wallet_balances import ADDRESS as ETH_ADDRESS
from tests.unit.test_wallet_balances import _rpc as rpc_client

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


# --- the loose verbs -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["I use PHP", "eu uso PHP", "i use CAD", "uso CAD", "I use TRY",
     "eu uso bitcoin", "i use ethereum", "uso ether"],
)
def test_use_with_a_code_or_a_coin_is_not_a_currency_preference(text: str) -> None:
    """Regression: "I use PHP" was stored as the Philippine peso, and "eu uso
    bitcoin" answered with the refusal meant for a currency."""
    intent = fact_intent(text)
    assert intent is None or intent.kind is not FactKind.PREFERRED_CURRENCY


@pytest.mark.parametrize(
    "text", ["Hi, my name is Leo and I use PHP", "Oi, me chamo Leo e uso ethereum"]
)
def test_an_introduction_that_uses_a_code_or_a_coin_saves_no_currency(text: str) -> None:
    intent = fact_intent(text)
    assert intent is not None
    kinds = {kind for kind, _ in intent.sets} | {intent.kind}
    assert FactKind.PREFERRED_CURRENCY not in kinds


@pytest.mark.parametrize(
    ("text", "value"),
    [("prefiro ver em BRL", "BRL"), ("show me prices in EUR", "EUR"),
     ("minha moeda é bitcoin", "bitcoin")],
)
def test_codes_and_coins_are_still_read_where_only_money_is_meant(text: str, value: str) -> None:
    intent = fact_intent(text)
    assert intent is not None and (intent.kind, intent.value) == (
        FactKind.PREFERRED_CURRENCY, value
    )


@pytest.mark.parametrize(
    ("text", "others"),
    [
        ("Oi, me chamo Leo, moro no Brasil e uso reais. Qual o preço do bitcoin?",
         {(FactKind.PREFERRED_NAME, "Leo"), (FactKind.HOME_ADDRESS, "Brasil")}),
        ("Oi, me chamo Leo e uso reais no dia a dia", {(FactKind.PREFERRED_NAME, "Leo")}),
    ],
)
def test_a_loose_currency_clause_may_run_on_inside_an_introduction(
    text: str, others: set[tuple[FactKind, str]]
) -> None:
    """Regression: the currency was dropped silently when anything followed
    it, while the other facts were saved and confirmed."""
    intent = fact_intent(text)
    assert intent is not None and intent.action is FactAction.SET_MANY
    assert set(intent.sets) == {*others, (FactKind.PREFERRED_CURRENCY, "reais")}


def test_moeda_preferida_starts_a_clause_of_its_own() -> None:
    intent = fact_intent("Oi, me chamo Leo, moeda preferida: BRL")
    assert intent is not None and intent.action is FactAction.SET_MANY
    assert set(intent.sets) == {
        (FactKind.PREFERRED_NAME, "Leo"), (FactKind.PREFERRED_CURRENCY, "BRL")
    }


def test_alone_a_loose_statement_must_end_at_the_currency() -> None:
    assert fact_intent("uso reais para pagar o aluguel") is None


# --- every dollar figure the chain tools render ------------------------------------

REAIS_EN = Conversion("BRL", BRL_RATE, EN)


def test_a_liquidity_position_values_holdings_and_fees_in_both() -> None:
    text = render_liquidity(OWNER, [ChainLiquidity(BASE, positions=(lp_position(),))], REAIS_EN)
    assert "≈ $5,400.00 (R$29,484.00)" in text
    assert "Uncollected: 0.01 WETH + 5 USDC ≈ $32.00 (R$174.72)" in text


def test_an_aave_account_values_totals_and_assets_in_both() -> None:
    chain = ChainLending(
        BASE,
        collateral_usd=Decimal("1000"), debt_usd=Decimal("400"),
        health_factor=Decimal("2"),
        assets=(lending_asset("WETH", "1", "0", "2000"), lending_asset("USDC", "0", "400", "1")),
    )
    text = render_lending(OWNER, [chain], REAIS_EN)
    assert "Collateral $1,000.00 (R$5,460.00) · Debt $400.00 (R$2,184.00)" in text
    assert "1 WETH ≈ $2,000.00 (R$10,920.00)" in text
    assert "400 USDC ≈ $400.00 (R$2,184.00)" in text


def test_wallet_activity_values_each_line_in_both_and_names_the_rate() -> None:
    chains = activity_chains(base_week())
    text = render_activity(WALLET, WINDOW, chains, BASE_KNOWN, EN, private=True,
                           conversion=REAIS_EN)
    assert "Aave: supplied 0.0222090 ETH (≈ $88.84 · R$485.04)" in text
    assert text.endswith(
        "_BRL at the daily reference rate: 1 USD = 5.4600 BRL (Frankfurter/ECB)._"
    )


class _Priced:
    async def usd_price(self, symbol: str) -> PricedAsset | None:
        return PricedAsset(symbol, Decimal(2000), "2026-09-18T10:00Z")


def _wallet_clearance(question: str) -> AuthorizedQuery:
    return EgressGuard().authorize(
        EgressRequest(
            asker=LEO,
            query=ProvenancedQuery(text=ETH_ADDRESS, origin=QueryOrigin.ASKER, question=question),
            provider=WalletProvider.server,
        )
    )


async def test_wallet_balances_value_native_and_pegged_tokens_in_both() -> None:
    rates = FixedRates()
    provider = WalletProvider(
        [ChainReader(ETHEREUM, "key", client=rpc_client({0: hex(10**18), 1: hex(5 * 10**6)}))],
        CallBudget(3),
        prices=_Priced(),
        rates=rates,
    )
    question = f"what does {ETH_ADDRESS} hold?"
    with (
        answering_with_facts(AskerFacts(LEO, preferred_currency="BRL")),
        authorized(_wallet_clearance(question)),
    ):
        result = await provider.call_tool(WALLET_TOOL, {"address": ETH_ADDRESS})

    assert "1.000000 ETH (~$2,000.00 · R$10,920.00 at 2026-09-18T10:00Z)" in result.text
    assert "5.000000 USDC (~$5.00 · R$27.30)" in result.text
    assert result.text.endswith("1 USD = 5.4600 BRL (Frankfurter/ECB)._")
    assert rates.asked == ["BRL"]


# --- the positions provider reads the asker's conversion -----------------------------


def _positions(rates: FixedRates) -> PositionsProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    return PositionsProvider(
        DEPLOYMENTS, "key", CallBudget(3), RateLimiter(0), client=client, timeout_seconds=2,
        rates=rates,
    )


async def test_a_positions_answer_carries_both_figures_and_one_footnote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def liquidity(self: UniswapReader, owner: str) -> ChainLiquidity:
        return ChainLiquidity(self._d.chain, positions=(lp_position(),))

    monkeypatch.setattr(UniswapReader, "liquidity", liquidity)
    rates = FixedRates()
    with answering_with_facts(AskerFacts(LEO, preferred_currency="BRL")):
        text = await _positions(rates).report(LIQUIDITY_TOOL, OWNER, "show my pools")

    assert text.count("≈ $5,400.00 (R$29,484.00)") == len(DEPLOYMENTS)
    assert text.count("1 USD = 5.4600 BRL") == 1, "the rate once, for the whole answer"
    assert rates.asked == ["BRL"]


async def test_activity_is_converted_in_the_language_the_answer_is_written_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def read(
        self: ActivityReader, address: str, window: ActivityWindow
    ) -> tuple[list[ChainActivity], dict[str, Recognised]]:
        return activity_chains(base_week()), BASE_KNOWN

    monkeypatch.setattr(ActivityReader, "read", read)
    # Detected as neither language; the renderer settles on Portuguese.
    question = f"movimentacoes {WALLET}"
    with answering_with_facts(AskerFacts(LEO, preferred_currency="BRL")):
        text = await _positions(FixedRates()).activity(
            Cleared((WALLET,), question=question, private=True)
        )

    assert "(≈ US$ 88,84 · R$ 485,04)" in text
    assert "_BRL pela taxa de referência diária: 1 USD = 5,4600 BRL" in text


async def test_a_portfolio_follow_up_is_converted_in_the_answers_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: "e no total?" is detected as neither language, and the
    dollars came out in Portuguese beside reais and a footnote in English."""
    held = ChainWallet(BASE, (Held("USDC", Decimal(1000), Decimal(1)),))
    wallet = WalletPortfolio(
        OWNER,
        (ChainPortfolio(held, ChainLiquidity(BASE), ChainLending(BASE)),),
    )

    async def read(self: PortfolioReader, address: str) -> WalletPortfolio:
        return wallet

    monkeypatch.setattr(PortfolioReader, "read", read)
    with answering_with_facts(AskerFacts(LEO, preferred_currency="BRL")):
        text = await _positions(FixedRates()).portfolio(
            Cleared((OWNER,), question="e no total?", private=True)
        )

    assert "US$ 1.000,00 (R$ 5.460,00)" in text
    assert "_BRL pela taxa de referência diária: 1 USD = 5,4600 BRL" in text
    assert "daily reference rate" not in text


# --- alerts ------------------------------------------------------------------------


class FakeCurrencies(PreferredCurrencies):
    """The owner's conversion, recording who was asked in which language."""

    def __init__(self, conversion: Conversion | None) -> None:
        self.conversion = conversion
        self.asked: list[tuple[PersonRef, Language]] = []

    async def conversion_for(self, person: PersonRef, language: Language) -> Conversion | None:
        self.asked.append((person, language))
        return self.conversion


def test_a_health_alert_shows_collateral_and_debt_in_both() -> None:
    firing = evaluate(health_alert(), hf("1.2"), NOW).firing
    assert firing is not None
    text = render_alert(firing, conversion=REAIS_EN)
    assert "Collateral $27,699.05 (R$151,236.81), debt $15,221.12 (R$83,107.32)" in text


async def test_the_sweep_prices_a_price_alert_for_its_owner_in_the_alerts_language() -> None:
    currencies = FakeCurrencies(REAIS)
    messenger = FakeMessenger()
    alert = price_alert(AlertState.BELOW)
    runner = AlertRunner(
        MemoryStore([alert]),
        FakeObserver({}),
        messenger,
        prices=FakeObserver({alert.id: btc("100000")}),
        currencies=currencies,
    )

    assert await runner.run_due(NOW) == 1
    [(_, _, text)] = messenger.sent
    assert "agora US$ 100.000,00 (R$ 546.000,00)" in text
    assert currencies.asked == [(alert.person, PT)], "a Portuguese alert, a Portuguese figure"


async def test_the_sweep_prices_an_english_health_alert_in_english() -> None:
    currencies = FakeCurrencies(REAIS_EN)
    messenger = FakeMessenger()
    health = health_alert()
    runner = AlertRunner(
        MemoryStore([health]),
        FakeObserver({health.id: hf("1.2")}),
        messenger,
        currencies=currencies,
    )

    assert await runner.run_due(NOW) == 1
    [(_, _, text)] = messenger.sent
    assert "(R$151,236.81)" in text
    assert currencies.asked == [(health.person, EN)]


def test_the_alert_sweep_is_built_with_the_owners_currency() -> None:
    runner = build_alert_runner(
        alert_settings(alerts_enabled=True, infura_key="k"), object(), AlertMessenger()  # type: ignore[arg-type]
    )
    assert runner is not None
    # The seam is private; what matters is that it is wired at all.
    assert isinstance(runner._currencies, PreferredCurrencies)


# --- the model is told, and a slow rate host is not waited on ------------------------


async def test_a_hanging_rate_host_is_given_up_on_within_the_timeout() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200)

    provider = FrankfurterProvider(
        CallBudget(4), ttl_seconds=0, timeout_seconds=0.05, transport=httpx.MockTransport(hang)
    )
    rates = UsdReferenceRates(provider)
    assert await asyncio.wait_for(rates.usd_to("BRL"), timeout=2) is None
