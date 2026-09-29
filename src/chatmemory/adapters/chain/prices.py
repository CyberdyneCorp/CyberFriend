"""The dollar value of a balance, from the same sources as the price tool.

In the same order, too: CoinGecko when a `COINGECKO_API_KEY` is configured
(its keyless endpoint refuses every caller), and the Chainlink feeds on
Ethereum mainnet when it is not or when it fails -- see
`adapters.market.coingecko`. Without an Infura key there are no feeds, and
CoinGecko is asked as before.

Why this does not go through the egress guard, stated plainly because it is
the kind of exception that becomes a hole if it is left implicit:

The guard exists to stop *content* leaving in an outbound query. This request
carries none. It asks CoinGecko for every asset this deployment knows about,
always, in a URL with no variable part -- the same constant request
`adapters.market.coingecko` deliberately makes, for the same reason: a request
that does not vary cannot reveal which asset anybody asked about. Nothing the
asker wrote, and nothing any message contained, is an input to it.

What the guard *does* cover is the wallet lookup itself, and that has already
happened by the time a figure is priced. This is arithmetic on a number
already in hand.
"""

from __future__ import annotations

import time
from decimal import Decimal

import httpx
import structlog

from chatmemory.adapters.chain.chainlink import CHAINLINK_LABEL, ChainlinkFeeds
from chatmemory.adapters.chain.provider import PricedAsset

log = structlog.get_logger()

COINGECKO_ENDPOINT = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_KEY_HEADER = "x-cg-demo-api-key"
COIN_IDS = {"ETH": "ethereum", "BTC": "bitcoin"}

ALIASES = {"WETH": "ETH"}
"""Wrapped ether is ether. Priced as its underlying rather than looked up
separately: a balance held as WETH is invisible to `eth_getBalance`, so
leaving it unpriced would show the one holding people most expect a figure
for as a bare number."""
DEFAULT_TTL_SECONDS = 60.0


class CoinGeckoPrices:
    """USD prices for the native assets, cached so a burst costs one call."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        endpoint: str = COINGECKO_ENDPOINT,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        api_key: str = "",
        chainlink: ChainlinkFeeds | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._chainlink = chainlink
        self._client = client
        self._transport = transport
        self._endpoint = endpoint
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._cache: dict[str, PricedAsset] = {}
        self._fetched_at = 0.0

    async def usd_price(self, symbol: str) -> PricedAsset | None:
        """The price, or None. Never raises: a missing price omits a figure."""
        key = ALIASES.get(symbol.upper(), symbol.upper())
        if key not in COIN_IDS:
            return None
        if self._cache and time.monotonic() - self._fetched_at < self._ttl:
            return self._cache.get(key)
        try:
            await self._refresh()
        except Exception as exc:  # noqa: BLE001 - a price must never fail a balance
            log.warning("chain.price_unavailable", error=self._redact(str(exc)))
            # A stale figure beats no figure, and it still states its own age.
            return self._cache.get(key)
        return self._cache.get(key)

    async def _refresh(self) -> None:
        if self._client is not None:
            await self._refresh_with(self._client)
            return
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            await self._refresh_with(client)

    def _redact(self, text: str) -> str:
        text = text.replace(self._api_key, "***") if self._api_key else text
        return self._chainlink.redact(text) if self._chainlink is not None else text

    async def _refresh_with(self, client: httpx.AsyncClient) -> None:
        fresh = await self._fresh(client)
        if fresh:
            self._cache = fresh
            self._fetched_at = time.monotonic()

    async def _fresh(self, client: httpx.AsyncClient) -> dict[str, PricedAsset]:
        """CoinGecko when it is asked and answers, otherwise the Chainlink feeds."""
        if self._chainlink is None:
            return await self._from_coingecko(client)
        if self._api_key:
            try:
                fresh = await self._from_coingecko(client)
            except Exception as exc:  # noqa: BLE001 - the feeds are the fallback
                log.warning(
                    "chain.coingecko_failed",
                    fallback=CHAINLINK_LABEL,
                    error=self._redact(str(exc))[:200],
                )
            else:
                if fresh:
                    return fresh
        readings = await self._chainlink.read(client)
        return {
            symbol: PricedAsset(
                symbol,
                r.price,
                r.updated_at.strftime("%Y-%m-%d %H:%MZ") + (" (stale)" if r.stale else ""),
                CHAINLINK_LABEL,
            )
            for symbol, r in readings.items()
            if symbol in COIN_IDS
        }

    async def _from_coingecko(self, client: httpx.AsyncClient) -> dict[str, PricedAsset]:
        response = await client.get(
            self._endpoint,
            params={
                # Constant, whichever asset is being priced. See the module
                # docstring: a request that does not vary reveals nothing.
                "ids": ",".join(COIN_IDS[s] for s in sorted(COIN_IDS)),
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
            },
            headers={COINGECKO_KEY_HEADER: self._api_key} if self._api_key else None,
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("coingecko answered something other than an object")
        fresh: dict[str, PricedAsset] = {}
        for symbol, coin in COIN_IDS.items():
            entry = payload.get(coin)
            if not isinstance(entry, dict):
                continue
            usd = entry.get("usd")
            if not isinstance(usd, int | float):
                continue
            updated = entry.get("last_updated_at")
            as_of = (
                time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(updated))
                if isinstance(updated, int | float)
                else "just now"
            )
            fresh[symbol] = PricedAsset(symbol, Decimal(str(usd)), as_of)
        return fresh
