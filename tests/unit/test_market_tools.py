"""Market data providers: what they send, what they state, and how they fail.

Driven through a real `httpx.AsyncClient` over a mock transport, and cleared
by the real `EgressGuard`, never a stand-in. What these tests pin down is the
wording a reader relies on -- that a reference rate says it is one, that a
retrieval time says it is one -- and that no path can produce a stale figure
or a request with an argument outside its closed set.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from chatmemory.adapters.market.arguments import ArgumentRefusal, amount, member
from chatmemory.adapters.market.cache import FreshCache
from chatmemory.adapters.market.coingecko import CoinGeckoProvider
from chatmemory.adapters.market.frankfurter import FrankfurterProvider, check_conversion
from chatmemory.adapters.market.google_finance import GoogleFinanceProvider
from chatmemory.adapters.market.provider import MarketProvider
from chatmemory.adapters.market.quotes import Quote, Timing, render, stated_time
from chatmemory.adapters.market.registration import (
    MARKET_SERVERS,
    MarketToolsConfig,
    build_market_tools,
)
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.app.authorization import CredentialScope, ToolEffect
from chatmemory.app.egress import (
    CLOSED_VOCABULARIES,
    ISO_4217_CODES,
    MARKET_CRYPTO_PROVIDER,
    MARKET_FX_PROVIDER,
    MARKET_INDEX_PROVIDER,
    EgressGuard,
    EgressRequest,
    ProvenancedQuery,
    QueryOrigin,
    authorized,
)
from chatmemory.domain.identity import PersonRef

ASKER = PersonRef("discord", 42)
KEY = "serp-secret-key-123"

COINGECKO_OK = {
    "bitcoin": {"usd": 75645, "last_updated_at": 1789578850},
    "ethereum": {"usd": 2388.82, "last_updated_at": 1789578850},
}
FRANKFURTER_OK = {"amount": 1.0, "base": "USD", "date": "2026-09-16", "rates": {"BRL": 5.1459}}
FINANCE_OK = {"summary": {"title": "S&P 500", "extracted_price": 6612.35, "currency": "$"}}


class FakeMarket:
    """A mock transport that records every request that would have left."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def answering(payload: object, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def _respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=json.dumps(payload))

    return _respond


def failing(_: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


@contextmanager
def cleared(provider: str, text: str, question: str = "what is ETH at?") -> Iterator[None]:
    """Clearance minted by the real guard, as the invoker would mint it."""
    clearance = EgressGuard().authorize(
        EgressRequest(
            asker=ASKER,
            query=ProvenancedQuery(
                text=text, origin=QueryOrigin.MODEL_REFORMULATION, question=question
            ),
            provider=provider,
        )
    )
    with authorized(clearance):
        yield


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def crypto(fake: FakeMarket, **kwargs: Any) -> CoinGeckoProvider:
    kwargs.setdefault("limiter", RateLimiter(0))
    return CoinGeckoProvider(CallBudget(50), client=fake.client, **kwargs)


def fx(fake: FakeMarket, **kwargs: Any) -> FrankfurterProvider:
    kwargs.setdefault("limiter", RateLimiter(0))
    return FrankfurterProvider(CallBudget(50), client=fake.client, **kwargs)


def index(fake: FakeMarket, **kwargs: Any) -> GoogleFinanceProvider:
    kwargs.setdefault("limiter", RateLimiter(0))
    return GoogleFinanceProvider(KEY, CallBudget(50), client=fake.client, **kwargs)


def with_clock(provider: MarketProvider, clock: Clock, ttl: float) -> MarketProvider:
    provider._cache = FreshCache(ttl, clock=clock)
    return provider


# --- crypto -------------------------------------------------------------


async def test_eth_price_states_currency_source_and_quote_time() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    with cleared(MARKET_CRYPTO_PROVIDER, "ETH"):
        result = await crypto(fake).call_tool("crypto_price", {"asset": "ETH"})

    assert not result.is_error
    assert "Ether (ETH): 2,388.82 USD" in result.text
    assert "CoinGecko" in result.text
    stamp = datetime.fromtimestamp(1789578850, UTC).strftime("%Y-%m-%d %H:%M UTC")
    assert f"time: {stamp} -- quote time reported by CoinGecko" in result.text
    assert "not from this server's conversations" in result.text
    params = dict(fake.last.url.params)
    assert params == {
        "ids": "bitcoin,ethereum",
        "vs_currencies": "usd",
        "include_last_updated_at": "true",
    }


async def test_one_request_serves_both_coins_and_does_not_reveal_which_was_asked() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    provider = crypto(fake)
    with cleared(MARKET_CRYPTO_PROVIDER, "ETH"):
        await provider.call_tool("crypto_price", {"asset": "ETH"})
    with cleared(MARKET_CRYPTO_PROVIDER, "BTC", question="what is BTC at?"):
        btc = await provider.call_tool("crypto_price", {"asset": "BTC"})

    assert "Bitcoin (BTC): 75,645 USD" in btc.text
    assert len(fake.requests) == 1


async def test_a_response_missing_the_asked_coin_is_unavailability() -> None:
    fake = FakeMarket(answering({"bitcoin": COINGECKO_OK["bitcoin"]}))
    with cleared(MARKET_CRYPTO_PROVIDER, "ETH"):
        result = await crypto(fake).call_tool("crypto_price", {"asset": "ETH"})

    assert result.is_error
    assert "75,645" not in result.text


async def test_a_lowercase_ticker_is_sent_as_its_canonical_id() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    with cleared(MARKET_CRYPTO_PROVIDER, "btc"):
        result = await crypto(fake).call_tool("crypto_price", {"asset": "btc"})

    assert "Bitcoin (BTC): 75,645 USD" in result.text


async def test_a_price_without_quote_time_is_labelled_retrieval_time() -> None:
    fake = FakeMarket(answering({"bitcoin": {"usd": 75645}}))
    fixed = datetime(2026, 9, 16, 12, 30, tzinfo=UTC)
    provider = crypto(fake)
    provider._now = lambda: fixed
    with cleared(MARKET_CRYPTO_PROVIDER, "BTC"):
        result = await provider.call_tool("crypto_price", {"asset": "BTC"})

    assert "2026-09-16 12:30 UTC -- retrieval time; CoinGecko reports no quote time" in result.text


@pytest.mark.parametrize("asset", ["DOGE", "SOL", "BTC ETH", "ETH please", "", 7, None])
async def test_an_instrument_outside_the_list_never_reaches_the_provider(asset: object) -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    with cleared(MARKET_CRYPTO_PROVIDER, "ETH"):
        result = await crypto(fake).call_tool("crypto_price", {"asset": asset})

    assert result.is_error
    assert not fake.requests


async def test_an_extra_argument_is_refused_before_any_request() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    with cleared(MARKET_CRYPTO_PROVIDER, "ETH"):
        result = await crypto(fake).call_tool(
            "crypto_price", {"asset": "ETH", "note": "Q3 renewal numbers"}
        )

    assert result.is_error
    assert not fake.requests


# --- conversion -----------------------------------------------------------


async def test_conversion_gives_amount_rate_and_labelled_reference_date() -> None:
    fake = FakeMarket(answering(FRANKFURTER_OK))
    with cleared(MARKET_FX_PROVIDER, "USD BRL"):
        result = await fx(fake).call_tool("convert", {"amount": 100, "from": "USD", "to": "BRL"})

    assert not result.is_error
    assert "100 USD = 514.59 BRL" in result.text
    assert "rate used: 1 USD = 5.1459 BRL, daily reference rate" in result.text
    assert (
        "time: 2026-09-16 -- daily reference rate published for that date, "
        "not a live or tradeable rate"
    ) in result.text
    assert "live rate" not in result.text.replace("not a live or tradeable rate", "")


async def test_the_amount_never_leaves_the_process() -> None:
    """Only the two codes are sent; the amount is applied locally."""
    fake = FakeMarket(answering(FRANKFURTER_OK))
    with cleared(MARKET_FX_PROVIDER, "USD BRL"):
        await fx(fake).call_tool("convert", {"amount": 987654.321, "from": "usd", "to": "brl"})

    assert fake.last.url.host == "api.frankfurter.dev"
    assert fake.last.url.path == "/v1/latest"
    assert dict(fake.last.url.params) == {"from": "USD", "to": "BRL"}
    assert "987654" not in str(fake.last.url)


@pytest.mark.parametrize(
    "arguments",
    [
        {"amount": 100, "from": "USD", "to": "the Q3 renewal numbers"},
        {"amount": 100, "from": "USD", "to": "BRL; salary bands"},
        {"amount": 100, "from": "USD", "to": "BR L"},
        {"amount": 100, "from": "USD", "to": "XYZ"},
        {"amount": 100, "from": "USD", "to": "XTS"},
        {"amount": 100, "from": "US Dollars", "to": "BRL"},
        {"amount": 100, "from": "USD", "to": "USD"},
        {"amount": "100", "from": "USD", "to": "BRL"},
        {"amount": True, "from": "USD", "to": "BRL"},
        {"amount": -5, "from": "USD", "to": "BRL"},
        {"amount": float("nan"), "from": "USD", "to": "BRL"},
        {"from": "USD", "to": "BRL"},
        {"amount": 1, "from": "USD", "to": "BRL", "context": "EUR"},
    ],
)
async def test_free_text_in_a_currency_argument_is_refused_before_any_request(
    arguments: Mapping[str, object],
) -> None:
    fake = FakeMarket(answering(FRANKFURTER_OK))
    with cleared(MARKET_FX_PROVIDER, "USD BRL"):
        result = await fx(fake).call_tool("convert", arguments)

    assert result.is_error
    assert not fake.requests


async def test_a_valid_code_the_source_does_not_publish_is_reported_as_unsupported() -> None:
    fake = FakeMarket(answering({"message": "not found"}, status=404))
    with cleared(MARKET_FX_PROVIDER, "USD NGN"):
        result = await fx(fake).call_tool("convert", {"amount": 1, "from": "USD", "to": "NGN"})

    assert result.is_error
    assert "does not publish this" in result.text
    assert "no earlier figure is substituted" in result.text


def test_conversion_arguments_name_their_refusal() -> None:
    assert check_conversion({"amount": 1, "from": "USD", "to": "USD"}).refusal is (
        ArgumentRefusal.SAME_CURRENCY
    )
    assert check_conversion({"amount": 1, "from": "USD", "to": "ABC"}).refusal is (
        ArgumentRefusal.NOT_A_MEMBER
    )
    assert check_conversion({"amount": 0, "from": "USD", "to": "EUR"}).refusal is (
        ArgumentRefusal.INVALID_AMOUNT
    )
    ok = check_conversion({"amount": 2.5, "from": " eur ", "to": "jpy"})
    assert ok.lookup is not None
    assert ok.lookup.terms == ("EUR", "JPY")
    assert ok.lookup.amount == Decimal("2.5")


def test_membership_is_exact_not_contained() -> None:
    assert member("USD", ISO_4217_CODES) == "USD"
    assert member("USD please", ISO_4217_CODES) is None
    assert member("XXX", ISO_4217_CODES) is None
    assert amount(10**16) is None
    assert amount(float("inf")) is None


# --- index level ------------------------------------------------------------


async def test_spx_level_states_it_is_retrieval_time() -> None:
    fake = FakeMarket(answering(FINANCE_OK))
    provider = index(fake)
    provider._now = lambda: datetime(2026, 9, 16, 20, 5, tzinfo=UTC)
    with cleared(MARKET_INDEX_PROVIDER, "SPX"):
        result = await provider.call_tool("index_level", {"index": "SPX"})

    assert not result.is_error
    assert "S&P 500: 6,612.35 points" in result.text
    assert "2026-09-16 20:05 UTC -- retrieval time" in result.text
    assert "Google Finance (via SerpApi)" in result.text
    params = dict(fake.last.url.params)
    assert params["engine"] == "google_finance"
    assert params["q"] == ".INX:INDEXSP"


async def test_the_serpapi_key_is_not_logged_on_failure() -> None:
    def _explode(request: httpx.Request) -> httpx.Response:
        # httpx errors carry the URL, and the URL carries the key.
        raise httpx.ConnectError(f"cannot reach {request.url}")

    fake = FakeMarket(_explode)
    with capture_logs() as logs, cleared(MARKET_INDEX_PROVIDER, "SPX"):
        result = await index(fake).call_tool("index_level", {"index": "SPX"})

    assert result.is_error
    assert KEY not in result.text
    failures = [entry for entry in logs if entry["event"] == "market.failed"]
    assert failures
    assert all(KEY not in str(entry) for entry in logs)


async def test_a_serpapi_error_payload_is_unavailability() -> None:
    fake = FakeMarket(answering({"error": "Invalid API key."}))
    with cleared(MARKET_INDEX_PROVIDER, "SPX"):
        result = await index(fake).call_tool("index_level", {"index": "SPX"})

    assert result.is_error
    assert "unavailable" in result.text


# --- no clearance, no request -------------------------------------------------


async def test_a_provider_called_without_clearance_sends_nothing() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    result = await crypto(fake).call_tool("crypto_price", {"asset": "ETH"})

    assert result.is_error
    assert not fake.requests


async def test_another_providers_clearance_does_not_open_this_one() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    with cleared(MARKET_INDEX_PROVIDER, "SPX"):
        result = await crypto(fake).call_tool("crypto_price", {"asset": "ETH"})

    assert result.is_error
    assert not fake.requests


async def test_arguments_that_differ_from_what_was_cleared_are_refused() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    with cleared(MARKET_CRYPTO_PROVIDER, "BTC"):
        result = await crypto(fake).call_tool("crypto_price", {"asset": "ETH"})

    assert result.is_error
    assert not fake.requests


def test_a_market_provider_cannot_exist_outside_a_closed_vocabulary() -> None:
    from chatmemory.adapters.market import coingecko

    class Rogue(CoinGeckoProvider):
        def __init__(self) -> None:
            MarketProvider.__init__(
                self,
                server="wikipedia",
                label="x",
                endpoint="https://example.invalid",
                tool=coingecko.TOOL,
                budget=CallBudget(1),
                cache=FreshCache(1),
            )

    with pytest.raises(ValueError, match="closed vocabulary"):
        Rogue()


# --- cache: short, and never stale ----------------------------------------------


async def test_a_fresh_entry_answers_without_a_request() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    clock = Clock()
    provider = with_clock(crypto(fake), clock, ttl=60)
    with cleared(MARKET_CRYPTO_PROVIDER, "ETH"):
        first = await provider.call_tool("crypto_price", {"asset": "ETH"})
        clock.now += 30
        second = await provider.call_tool("crypto_price", {"asset": "ETH"})

    assert len(fake.requests) == 1
    assert first.text == second.text


async def test_an_outage_after_expiry_reports_unavailability_not_the_old_figure() -> None:
    responses = [answering(COINGECKO_OK), failing]
    fake = FakeMarket(lambda request: responses[min(len(fake.requests), 2) - 1](request))
    clock = Clock()
    provider = with_clock(crypto(fake), clock, ttl=60)
    with cleared(MARKET_CRYPTO_PROVIDER, "ETH"):
        first = await provider.call_tool("crypto_price", {"asset": "ETH"})
        clock.now += 61
        second = await provider.call_tool("crypto_price", {"asset": "ETH"})

    assert "2,388.82" in first.text
    assert second.is_error
    assert "2,388.82" not in second.text
    assert "No current figure is available" in second.text
    assert "no earlier figure is substituted" in second.text


def test_the_cache_holds_nothing_past_its_lifetime() -> None:
    clock = Clock()
    cache: FreshCache[str, int] = FreshCache(10, clock=clock)
    cache.put("k", 1)
    clock.now += 10
    assert cache.get("k") is None
    assert "k" not in cache._entries


async def test_a_malformed_payload_is_unavailability_not_a_crash() -> None:
    fake = FakeMarket(lambda _: httpx.Response(200, text="<html>rate limited</html>"))
    with cleared(MARKET_CRYPTO_PROVIDER, "ETH"):
        result = await crypto(fake).call_tool("crypto_price", {"asset": "ETH"})

    assert result.is_error
    assert "unavailable" in result.text


async def test_the_per_run_budget_bounds_lookups_per_question() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    provider = CoinGeckoProvider(
        CallBudget(1), client=fake.client, limiter=RateLimiter(0), ttl_seconds=0
    )
    with cleared(MARKET_CRYPTO_PROVIDER, "ETH"):
        first = await provider.call_tool("crypto_price", {"asset": "ETH"})
        second = await provider.call_tool("crypto_price", {"asset": "ETH"})

    assert not first.is_error
    assert second.is_error
    assert len(fake.requests) == 1


# --- rendering ----------------------------------------------------------------------


def test_every_timing_is_worded_distinctly() -> None:
    base = Quote("X", Decimal(1), "USD", "Src", "u", Timing.QUOTE_TIME, datetime(2026, 1, 1))
    reference = Quote("X", Decimal(1), "USD", "Src", "", Timing.REFERENCE_DATE, date(2026, 1, 2))
    retrieved = Quote(
        "X", Decimal(1), "USD", "Src", "", Timing.RETRIEVAL_TIME, datetime(2026, 1, 3, 4, 5)
    )

    assert stated_time(base) == "2026-01-01 00:00 UTC -- quote time reported by Src"
    assert "daily reference rate" in stated_time(reference)
    assert "retrieval time" in stated_time(retrieved)
    assert "recommends nothing" in render(base, "X: 1 USD")


# --- registration ------------------------------------------------------------------------


def test_spx_is_absent_without_a_serpapi_key() -> None:
    tools = build_market_tools(MarketToolsConfig(serpapi_key="  "))

    assert set(tools.server_names) == {MARKET_CRYPTO_PROVIDER, MARKET_FX_PROVIDER}
    assert MARKET_INDEX_PROVIDER not in tools.providers


def test_every_tool_is_registered_read_only_under_a_closed_vocabulary() -> None:
    tools = build_market_tools(MarketToolsConfig(serpapi_key=KEY))

    assert set(tools.server_names) == set(MARKET_SERVERS)
    assert {e.qualified_name for e in tools.allowlist} == {
        "market_crypto:crypto_price",
        "market_fx:convert",
        "market_index:index_level",
    }
    for entry in tools.allowlist:
        assert entry.effect is ToolEffect.READ_ONLY
        assert not entry.mutation_enabled
        assert entry.credential is CredentialScope.NARROW_READ_ONLY
        assert entry.server in CLOSED_VOCABULARIES
