"""Bitcoin and Ether prices from CoinGecko.

No key, and a quote timestamp (`last_updated_at`) on every price, which is
the property that matters most: the figure can be dated by the source itself
rather than by when we happened to ask.

The instrument vocabulary is the ticker a person uses ("ETH"); CoinGecko
wants its own coin id ("ethereum"). The mapping is a constant here, so what
leaves is still a function of a closed-set member and nothing else.

`latest` is the one way in that is not a tool call: the price-alert sweep has
no asker, so no clearance, and asks for every coin at once. It sends the very
request a tool call sends -- a constant, see `fetch` -- and shares the cache,
the rate limit and the client, so it is this provider, not a second one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType

import httpx

from chatmemory.adapters.market.arguments import ArgumentCheck, Lookup, single_member
from chatmemory.adapters.market.cache import FreshCache
from chatmemory.adapters.market.provider import (
    DEFAULT_TIMEOUT,
    MarketProvider,
    MarketToolSpec,
    ProviderUnavailable,
    as_decimal,
    read_json,
)
from chatmemory.adapters.market.quotes import Quote, Timing
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.results import as_mapping
from chatmemory.app.currency import UsdRates, asker_conversion, rate_note
from chatmemory.app.egress import CRYPTO_ASSETS, MARKET_CRYPTO_PROVIDER

COINGECKO_ENDPOINT = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_LABEL = "CoinGecko"
QUOTE_CURRENCY = "usd"
DEFAULT_TTL_SECONDS = 60.0

CRYPTO_PRICE = "crypto_price"
ARG_ASSET = "asset"

COIN_IDS: Mapping[str, str] = MappingProxyType({"BTC": "bitcoin", "ETH": "ethereum"})
COIN_PAGES: Mapping[str, str] = MappingProxyType(
    {
        "BTC": "https://www.coingecko.com/en/coins/bitcoin",
        "ETH": "https://www.coingecko.com/en/coins/ethereum",
    }
)
_NAMES: Mapping[str, str] = MappingProxyType({"BTC": "Bitcoin (BTC)", "ETH": "Ether (ETH)"})

TOOL = MarketToolSpec(
    name=CRYPTO_PRICE,
    description=(
        "Current price in US dollars of the cryptocurrency Bitcoin (BTC) or "
        "Ether (ETH, ethereum), from live crypto market data, with the time "
        "of the quote. Use for what BTC or ETH is at now, not for prices "
        "someone mentioned in a channel."
    ),
    input_schema=MappingProxyType(
        {
            "type": "object",
            "properties": {
                ARG_ASSET: {
                    "type": "string",
                    "enum": sorted(CRYPTO_ASSETS),
                    "description": "The ticker: BTC or ETH. Nothing else is accepted.",
                }
            },
            "required": [ARG_ASSET],
            "additionalProperties": False,
        }
    ),
)


class CoinGeckoProvider(MarketProvider):
    """`ToolSession` over CoinGecko's simple price endpoint."""

    def __init__(
        self,
        budget: CallBudget,
        *,
        endpoint: str = COINGECKO_ENDPOINT,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        limiter: RateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        rates: UsdRates | None = None,
    ) -> None:
        #: Where the asker's preferred currency is priced from; None adds nothing.
        self._rates = rates
        super().__init__(
            server=MARKET_CRYPTO_PROVIDER,
            label=COINGECKO_LABEL,
            endpoint=endpoint,
            tool=TOOL,
            budget=budget,
            cache=FreshCache(ttl_seconds),
            limiter=limiter,
            timeout_seconds=timeout_seconds,
            client=client,
            transport=transport,
        )

    def check_arguments(self, arguments: Mapping[str, object]) -> ArgumentCheck:
        return single_member(arguments, ARG_ASSET, CRYPTO_ASSETS)

    async def annotate(self, line: str, quote: Quote, question: str) -> str:
        """The dollar price, then the same price in the asker's preferred currency.

        The rate's own line says what it is: a daily reference rate, which the
        converted figure is only as current as.
        """
        conversion = await asker_conversion(self._rates, question)
        if conversion is None:
            return line
        return f"{line} ({conversion.shown(quote.value)})\n{rate_note(conversion)}"

    async def latest(self) -> Mapping[str, Quote]:
        """Every supported coin's fresh quote, by ticker, for the alert sweep.

        Not through `call_tool`, so not through the egress guard, for the
        reason `adapters.chain.prices` gives: the request is constant, carries
        nothing anybody wrote, and cannot reveal what anybody watches. A
        fresh cache entry answers without a request; an expired one is never
        returned, so this raises `ProviderUnavailable` rather than hand an
        alert a stale price. One coin missing from a response is not every
        coin missing: the ones it did carry are cached by `fetch` and returned.
        """
        missing = [a for a in sorted(COIN_IDS) if self._cache.get((a,)) is None]
        if missing:
            # Whatever the response did carry is in the cache either way.
            with contextlib.suppress(ProviderUnavailable):
                await self._refresh(missing[0])
        quotes = {a: q for a in sorted(COIN_IDS) if (q := self._cache.get((a,))) is not None}
        if not quotes:
            raise ProviderUnavailable("coingecko returned no usable price")
        return quotes

    async def _refresh(self, asset: str) -> None:
        """One request, which fills the cache for every coin it carries."""
        if not await self._limiter.acquire():
            raise ProviderUnavailable("coingecko rate limited")
        async with asyncio.timeout(self._timeout):
            quote = await self._fetch_with_client(Lookup(terms=(asset,)))
        self._cache.put((asset,), quote)

    async def fetch(self, lookup: Lookup, client: httpx.AsyncClient) -> Quote:
        (asset,) = lookup.terms
        response = await client.get(
            self.endpoint,
            params={
                # Every supported coin, whichever was asked for. The request is
                # then a constant: it does not reveal which asset someone
                # asked about, and one call fills the cache for both, which
                # halves what a burst of questions costs the free tier's
                # rate limit.
                "ids": ",".join(COIN_IDS[a] for a in sorted(COIN_IDS)),
                "vs_currencies": QUOTE_CURRENCY,
                "include_last_updated_at": "true",
            },
            timeout=self._timeout,
        )
        payload = read_json(response, COINGECKO_LABEL)
        quotes = {a: self._quote(a, as_mapping(payload.get(COIN_IDS[a]))) for a in COIN_IDS}
        for other, quote in quotes.items():
            if other != asset and quote is not None:
                self.remember(Lookup(terms=(other,)), quote)
        requested = quotes[asset]
        if requested is None:
            raise ProviderUnavailable("coingecko returned no usable price")
        return requested

    def _quote(self, asset: str, entry: Mapping[str, object]) -> Quote | None:
        price = as_decimal(entry.get(QUOTE_CURRENCY))
        if price is None:
            return None
        timing, as_of = self._when(entry.get("last_updated_at"))
        return Quote(
            instrument=_NAMES[asset],
            value=price,
            unit=QUOTE_CURRENCY.upper(),
            source=COINGECKO_LABEL,
            url=COIN_PAGES[asset],
            timing=timing,
            as_of=as_of,
        )

    def _when(self, quoted_at: object) -> tuple[Timing, datetime]:
        # The quote time when CoinGecko gives one. If a response ever lacks
        # it, the retrieval time is stated as retrieval time -- never
        # presented as the quote's own time.
        if isinstance(quoted_at, int) and not isinstance(quoted_at, bool) and quoted_at > 0:
            return Timing.QUOTE_TIME, datetime.fromtimestamp(quoted_at, UTC)
        return Timing.RETRIEVAL_TIME, self.retrieved_at()
