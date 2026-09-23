"""The clock every service reads "now" from.

One definition, so the default the reasoning stages, catch-up, the asks
service and the bot's loops fall back on is the same function the process's
`Edges.clock` holds in production. A test that fixes the clock replaces this
and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The UTC wall clock, which every process reads unless handed another."""
    return datetime.now(UTC)
