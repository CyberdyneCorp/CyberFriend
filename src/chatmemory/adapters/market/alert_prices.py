"""Price alerts' readings: the market tools' CoinGecko provider, read once a sweep.

Implements both `PriceFeed` (the baseline a price-alert confirmation shows)
and `PositionObserver` (a sweep's readings) over one `CoinGeckoProvider`, so a
price alert reaches no host the market tools do not already reach, and every
due price alert, BTC and ETH alike, is answered by one request -- the constant
one described in `adapters.market.coingecko`.

What leaves carries nothing about anybody: not the asset watched, not the
level, not who watches it. That is why this path needs no clearance, and is
declared in the `alert-kinds` spec rather than routed through the guard.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time

import structlog

from chatmemory.adapters.market.coingecko import COINGECKO_LABEL, CoinGeckoProvider
from chatmemory.adapters.market.quotes import Quote
from chatmemory.ports.alerts import (
    Observation,
    PositionAlert,
    PriceObservation,
    ReadFailure,
)

log = structlog.get_logger()


class AlertPrices:
    """Implements `PriceFeed` and `PositionObserver` for price alerts."""

    def __init__(self, provider: CoinGeckoProvider) -> None:
        self._provider = provider

    async def latest(self) -> Mapping[str, PriceObservation]:
        quotes = await self._provider.latest()
        return {asset: _observation(asset, quote) for asset, quote in quotes.items()}

    async def observe(self, alerts: Sequence[PositionAlert]) -> Mapping[int, Observation]:
        try:
            prices = await self.latest()
        except Exception as exc:  # noqa: BLE001 - a failed read is a failure per alert
            log.warning("alerts.prices_failed", alerts=len(alerts), error=str(exc)[:200])
            return {a.id: ReadFailure("price unavailable") for a in alerts}
        out: dict[int, Observation] = {}
        for alert in alerts:
            asset = alert.price.asset if alert.price is not None else ""
            out[alert.id] = prices.get(asset) or ReadFailure("no price for this asset")
        return out


def _observation(asset: str, quote: Quote) -> PriceObservation:
    moment = quote.as_of
    if not isinstance(moment, datetime):
        # A daily figure; CoinGecko's are always timed, but `Quote` allows it.
        moment = datetime.combine(moment, time(), UTC)
    return PriceObservation(asset=asset, price=quote.value, as_of=moment, source=COINGECKO_LABEL)
