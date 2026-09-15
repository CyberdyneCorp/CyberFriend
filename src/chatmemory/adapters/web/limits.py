"""How often the agent may reach the internet, and how much per question.

Two different caps, for two different failures:

*   **Rate.** Wikipedia and SerpApi are shared resources with etiquette and
    with quotas. A loop that retries a slightly reworded query is normal
    behaviour for a reasoning agent and abnormal behaviour for an API client,
    so the spacing is enforced here rather than hoped for upstream.
*   **Per run.** A question that causes twenty outbound calls is either a
    confused loop or a steered one. The cap bounds both, and it is shared
    across providers because the thing being bounded is *egress*, not any one
    provider's quota.

The per-run budget is keyed by the asking person's question. That is not an
arbitrary choice: the question is the only run identity that reaches the
egress boundary, since a provider session outlives any single run and is
handed nothing but a tool name and arguments. Two people asking the exact
same question within the window share a budget, which errs towards fewer
outbound calls.

The window is what makes the key honest. A run is a few seconds long, so
spend older than that belonged to a different run and holding it against the
next one would mean asking the same question tomorrow gets no search at all.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from hashlib import sha256

import structlog

log = structlog.get_logger()

DEFAULT_MIN_INTERVAL = 0.2
DEFAULT_MAX_WAIT = 5.0
DEFAULT_CALLS_PER_RUN = 4
DEFAULT_RUN_WINDOW = 300.0
REMEMBERED_RUNS = 256


class RateLimiter:
    """Minimum spacing between outbound calls to one provider.

    `acquire` reserves its slot under the lock and sleeps outside it, so N
    waiters are spaced N intervals apart rather than all waking together and
    then racing. It returns False instead of sleeping past `max_wait`: a call
    that would sit in a queue longer than the answer can wait for is better
    reported as a degradation than left to expire as a timeout.
    """

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_interval < 0:
            raise ValueError("min_interval cannot be negative")
        self._min_interval = min_interval
        self._max_wait = max_wait
        self._clock = clock
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> bool:
        async with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_at - now)
            if wait > self._max_wait:
                return False
            self._next_at = max(now, self._next_at) + self._min_interval
        if wait > 0:
            await asyncio.sleep(wait)
        return True


class CallBudget:
    """A cap on how many outbound calls one question may cause.

    Deliberately not a counter that is reset by a caller: a reset method is a
    method the reasoning loop could be steered into calling. Old keys fall
    out of a bounded LRU instead, so the budget cannot grow without limit and
    cannot be cleared on demand.
    """

    def __init__(
        self,
        max_calls_per_run: int = DEFAULT_CALLS_PER_RUN,
        *,
        window_seconds: float = DEFAULT_RUN_WINDOW,
        remember: int = REMEMBERED_RUNS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_calls_per_run < 1:
            raise ValueError("a run must be able to make at least one call")
        self._max = max_calls_per_run
        self._window = window_seconds
        self._remember = remember
        self._clock = clock
        self._spent: OrderedDict[str, tuple[int, float]] = OrderedDict()

    @property
    def max_calls_per_run(self) -> int:
        return self._max

    def spend(self, question: str) -> bool:
        """Charge one call to this question. False when it has none left."""
        key = _key(question)
        used, started = self._spent.get(key, (0, self._clock()))
        now = self._clock()
        if now - started > self._window:
            # A new run asking the same thing, not the old run continuing.
            used, started = 0, now
        if used >= self._max:
            return False
        self._spent[key] = (used + 1, started)
        self._spent.move_to_end(key)
        while len(self._spent) > self._remember:
            self._spent.popitem(last=False)
        return True

    def spent_for(self, question: str) -> int:
        used, started = self._spent.get(_key(question), (0, self._clock()))
        return 0 if self._clock() - started > self._window else used


def _key(question: str) -> str:
    return sha256(question.strip().lower().encode("utf-8")).hexdigest()
