"""A short-lived cache that can never answer with a stale figure.

The cache exists for the free tiers: CoinGecko rate-limits, SerpApi spends
quota, and a burst of people asking "what's BTC at" within a minute should
cost one request. It does not exist to paper over an outage.

That is the one rule here, and it is structural rather than a flag: an entry
past its lifetime is deleted on read and cannot be returned by any method, so
there is no "serve stale on error" path for a later change to switch on. A
provider that fails after its entry expired reports that it is unavailable.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

DEFAULT_ENTRIES = 64


class FreshCache(Generic[K, V]):
    """Entries live for `ttl_seconds` and are then gone."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        max_entries: int = DEFAULT_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("a cache lifetime cannot be negative")
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        found = self._entries.get(key)
        if found is None:
            return None
        expires_at, value = found
        if self._clock() >= expires_at:
            # Deleted, not merely skipped: an expired figure that is still
            # held is one a future "fallback" could reach for.
            del self._entries[key]
            return None
        return value

    def put(self, key: K, value: V) -> None:
        if self._ttl == 0:
            return
        self._entries[key] = (self._clock() + self._ttl, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
