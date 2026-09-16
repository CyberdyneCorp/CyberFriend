"""The S&P 500 level, from Google Finance through SerpApi.

Present only with `SERPAPI_KEY`, for the reason the web search provider
gives: a tool registered without its key would be offered to every run and
fail every call. Without the key there is no server, no allowlist entry and
nothing for the router to offer.

Google Finance's summary carries a level and no quote time, so the time
stated is when we retrieved it, labelled as retrieval time. Each lookup
spends SerpApi quota shared with web search, which is why the cache lifetime
is longer here than for crypto.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from chatmemory.adapters.web.results import as_mapping, as_text
from chatmemory.app.egress import MARKET_INDEX_PROVIDER, MARKET_INDICES

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
GOOGLE_FINANCE_LABEL = "Google Finance (via SerpApi)"
DEFAULT_TTL_SECONDS = 300.0

INDEX_LEVEL = "index_level"
ARG_INDEX = "index"

GOOGLE_SYMBOLS: Mapping[str, str] = MappingProxyType({"SPX": ".INX:INDEXSP"})
_NAMES: Mapping[str, str] = MappingProxyType({"SPX": "S&P 500"})

TOOL = MarketToolSpec(
    name=INDEX_LEVEL,
    description=(
        "Current level of the S&P 500 stock market index (SPX, S&P, SP500), "
        "from Google Finance, with the time it was retrieved."
    ),
    input_schema=MappingProxyType(
        {
            "type": "object",
            "properties": {
                ARG_INDEX: {
                    "type": "string",
                    "enum": sorted(MARKET_INDICES),
                    "description": "The index: SPX for the S&P 500. Nothing else is accepted.",
                }
            },
            "required": [ARG_INDEX],
            "additionalProperties": False,
        }
    ),
)


class GoogleFinanceProvider(MarketProvider):
    """`ToolSession` over SerpApi's Google Finance engine."""

    def __init__(
        self,
        api_key: str,
        budget: CallBudget,
        *,
        endpoint: str = SERPAPI_ENDPOINT,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        limiter: RateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GoogleFinanceProvider requires an API key; omit the provider instead")
        super().__init__(
            server=MARKET_INDEX_PROVIDER,
            label=GOOGLE_FINANCE_LABEL,
            endpoint=endpoint,
            tool=TOOL,
            budget=budget,
            cache=FreshCache(ttl_seconds),
            limiter=limiter,
            timeout_seconds=timeout_seconds,
            client=client,
            secret_values=(api_key,),
        )
        self._api_key = api_key

    def check_arguments(self, arguments: Mapping[str, object]) -> ArgumentCheck:
        return single_member(arguments, ARG_INDEX, MARKET_INDICES)

    async def fetch(self, lookup: Lookup, client: httpx.AsyncClient) -> Quote:
        (index,) = lookup.terms
        symbol = GOOGLE_SYMBOLS[index]
        response = await client.get(
            self.endpoint,
            params={"engine": "google_finance", "q": symbol, "api_key": self._api_key},
            timeout=self._timeout,
        )
        # Taken before parsing, as close to the answer's arrival as possible.
        retrieved = self.retrieved_at()
        payload = read_json(response, GOOGLE_FINANCE_LABEL)
        if as_text(payload.get("error")):
            raise ProviderUnavailable("serpapi reported an error for this lookup")
        level = as_decimal(as_mapping(payload.get("summary")).get("extracted_price"))
        if level is None:
            raise ProviderUnavailable("google finance returned no usable level")
        return Quote(
            instrument=_NAMES[index],
            value=level,
            unit="points",
            source=GOOGLE_FINANCE_LABEL,
            url=f"https://www.google.com/finance/quote/{symbol}",
            timing=Timing.RETRIEVAL_TIME,
            as_of=retrieved,
        )
