"""Market data: current prices, index levels and exchange rates.

A price in a channel is a record of what someone said, not a price. These
providers answer "what is BTC at" from live sources instead, and every figure
states the time it refers to -- or, where the source gives none, the time it
was retrieved, labelled as such.

They are governed exactly like the web providers: each satisfies
`ToolSession`, so the allowlist, routing, invoke-time authorization and audit
apply unchanged. The one thing that differs is how the egress guard clears
them. Their arguments are members of closed vocabularies -- two tickers, one
index, ISO 4217 codes -- so the guard checks membership in the fixed sets in
`app.egress.CLOSED_VOCABULARIES` instead of rooting in the asker's words, and
refuses anything outside them before a request is made.

Read in dependency order:

    arguments        what a tool accepts, checked before anything is sent
    quotes           a figure and the time it refers to
    cache            short-lived, and never stale
    provider         one source as a governed, degrading tool session
    coingecko        BTC and ETH, with the quote time
    frankfurter      conversion at the ECB daily reference rate, amount kept local
    google_finance   the S&P 500, present only with a SerpApi key
    registration     servers + allowlist + factory, for `connect()`
"""

from __future__ import annotations

from chatmemory.adapters.market.arguments import ArgumentCheck, ArgumentRefusal, Lookup
from chatmemory.adapters.market.cache import FreshCache
from chatmemory.adapters.market.coingecko import CRYPTO_PRICE, CoinGeckoProvider
from chatmemory.adapters.market.frankfurter import CONVERT, FrankfurterProvider
from chatmemory.adapters.market.google_finance import INDEX_LEVEL, GoogleFinanceProvider
from chatmemory.adapters.market.provider import MarketProvider, MarketToolSpec
from chatmemory.adapters.market.quotes import Quote, Timing
from chatmemory.adapters.market.registration import (
    MARKET_SERVERS,
    MarketTools,
    MarketToolsConfig,
    build_market_tools,
)

__all__ = [
    "CONVERT",
    "CRYPTO_PRICE",
    "INDEX_LEVEL",
    "MARKET_SERVERS",
    "ArgumentCheck",
    "ArgumentRefusal",
    "CoinGeckoProvider",
    "FrankfurterProvider",
    "FreshCache",
    "GoogleFinanceProvider",
    "Lookup",
    "MarketProvider",
    "MarketToolSpec",
    "MarketTools",
    "MarketToolsConfig",
    "Quote",
    "Timing",
    "build_market_tools",
]
