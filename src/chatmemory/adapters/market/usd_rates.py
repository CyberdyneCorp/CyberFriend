"""US dollar rates for the asker's preferred currency, from the FX provider.

Implements `app.currency.UsdRates` over the market tools' Frankfurter
provider -- the same class, host and request as a conversion the asker asks
for -- so a second figure reaches no host the conversion tool does not.

The rate is a daily ECB reference rate, so a short cache loses nothing: ten
minutes, and one lookup then serves every figure in an answer and every answer
in the next few minutes. A rate that cannot be read is None, and never a
fallback: the answer is then in dollars alone.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal

import structlog

from chatmemory.adapters.market.cache import FreshCache
from chatmemory.adapters.market.frankfurter import FrankfurterProvider
from chatmemory.domain.currency import SUPPORTED_CODES, USD

log = structlog.get_logger()

DEFAULT_TTL_SECONDS = 600.0


class UsdReferenceRates:
    """Implements `UsdRates`: USD to a supported code, cached briefly."""

    def __init__(
        self,
        provider: FrankfurterProvider,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._cache: FreshCache[str, Decimal] = FreshCache(ttl_seconds, clock=clock)

    async def usd_to(self, code: str) -> Decimal | None:
        # Checked here as well as by the fact's validation: this is the last
        # point before a code leaves, and only a member of the set may.
        if code == USD or code not in SUPPORTED_CODES:
            return None
        cached = self._cache.get(code)
        if cached is not None:
            return cached
        try:
            rate = await self._provider.reference_rate(USD, code)
        except Exception as exc:  # noqa: BLE001 - no rate is a dollar-only answer
            log.warning(
                "market.usd_rate_unavailable",
                currency=code,
                error=self._provider.redact(str(exc))[:200],
            )
            return None
        self._cache.put(code, rate)
        return rate
