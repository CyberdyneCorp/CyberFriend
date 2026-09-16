"""What a market figure is, and how it says how current it is.

A price with no time attached is the failure this capability exists to
prevent: a figure a reader cannot date reads as "now", whether it came from a
channel message last month or a cache entry from an hour ago. So the time is
part of the value, not an optional decoration, and the three kinds of time a
source can give are kept distinct all the way to the rendered text:

*   `QUOTE_TIME` -- the source says when the price was observed.
*   `REFERENCE_DATE` -- a published daily reference rate, which is a record of
    a fixing and must never be read as a rate someone could trade at now.
*   `RETRIEVAL_TIME` -- the source gave no time, so the only honest time is
    when we asked, and it is labelled as that rather than passed off as the
    quote's own.

The rendered block opens by naming what it is, like a web result does, so the
distinction from the team's conversations survives being quoted into a prompt
as fenced data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum


class Timing(StrEnum):
    """What the time on a quote refers to."""

    QUOTE_TIME = "quote_time"
    REFERENCE_DATE = "reference_date"
    RETRIEVAL_TIME = "retrieval_time"


@dataclass(frozen=True, slots=True)
class Quote:
    """One figure from one source, with the time it refers to."""

    instrument: str
    value: Decimal
    unit: str
    source: str
    url: str
    timing: Timing
    as_of: datetime | date


def figure(value: Decimal) -> str:
    """A number as a person reads it: grouped, never in exponent form."""
    return f"{value:,f}"


def money(value: Decimal) -> str:
    """An amount of money, to the cent."""
    return f"{value.quantize(Decimal('0.01')):,f}"


def stated_time(quote: Quote) -> str:
    """The time line, worded so its meaning cannot be mistaken."""
    if quote.timing is Timing.REFERENCE_DATE:
        day = quote.as_of.date() if isinstance(quote.as_of, datetime) else quote.as_of
        return (
            f"{day.isoformat()} -- daily reference rate published for that date, "
            "not a live or tradeable rate"
        )
    moment = _utc(quote.as_of)
    if quote.timing is Timing.QUOTE_TIME:
        return f"{moment} -- quote time reported by {quote.source}"
    return f"{moment} -- retrieval time; {quote.source} reports no quote time"


def render(quote: Quote, line: str) -> str:
    """A quote as data: what it is, the figure, its time, and its source."""
    return "\n".join(
        (
            f"Market data from {quote.source}. This is an external live source, "
            "not from this server's conversations. It reports a figure and "
            "recommends nothing.",
            line,
            f"time: {stated_time(quote)}",
            f"source: {quote.source} {quote.url}".rstrip(),
        )
    )


def _utc(moment: datetime | date) -> str:
    if not isinstance(moment, datetime):
        return moment.isoformat()
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
