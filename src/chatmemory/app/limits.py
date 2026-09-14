"""Per-person rate limiting.

Any member can now trigger a reasoning run, so cost is driven by user
behaviour rather than our own scheduling. Limits are keyed on the person, not
the surface, so asking in a DM does not reset a slash-command allowance.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from chatmemory.domain.identity import PersonRef


@dataclass(frozen=True, slots=True)
class LimitDecision:
    allowed: bool
    retry_after_seconds: float = 0.0


class RateLimiter:
    """Sliding-window limiter, one window per person across every surface."""

    def __init__(self, max_questions: int = 20, window_seconds: float = 3600.0) -> None:
        self._max = max_questions
        self._window = window_seconds
        self._events: dict[PersonRef, deque[float]] = defaultdict(deque)

    def check(self, person: PersonRef, now: float | None = None) -> LimitDecision:
        """Test and record in one step, so concurrent asks cannot both pass."""
        current = time.monotonic() if now is None else now
        events = self._events[person]
        cutoff = current - self._window
        while events and events[0] <= cutoff:
            events.popleft()

        if len(events) >= self._max:
            return LimitDecision(False, retry_after_seconds=events[0] + self._window - current)

        events.append(current)
        return LimitDecision(True)
