"""Putting already-extracted history back in the extraction queue, for decisions.

History the ask pass read before decisions existed is recorded as extracted,
so the backlog worker never revisits it and every decision in it stays
unanswerable. Resetting the whole watermark would fix that by re-paying the
entire ask history, one model call per candidate message. This resets it only
for the messages that could hold a decision: live, inside the window the
operator names, in a channel in indexing scope, and carrying one of the
candidate filter's `DECISION_MARKERS`.

The matching is done here, in Python, with the filter's own compiled pattern
rather than a Postgres regex built from it. The two dialects disagree on `\\b`
and on case folding outside ASCII, so a translated pattern would reset a set
of messages the filter then declines to send to the model -- or miss ones it
would have sent. Nothing the operator types reaches SQL except as a bound
parameter.

Nothing is extracted here. The reset messages are pending again, and the
`BacklogExtractionWorker` already running in ingest drains them at its rate
bound, through the same pipeline as everything else.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from chatmemory.app.asks.candidates import DECISION_MARKERS

#: How many messages one scan reads. Only ids and text come back, and the
#: backfill is a one-off, so this bounds memory rather than time.
SCAN_PAGE = 500


@dataclass(frozen=True, slots=True)
class ScannedMessage:
    platform_message_id: int
    content: str


class BackfillLedger(Protocol):
    async def live_messages_since(
        self,
        since: datetime,
        channel_ids: frozenset[int],
        after_id: int,
        limit: int,
    ) -> Sequence[ScannedMessage]:
        """Live messages in `channel_ids` said at or after `since`, by id, after `after_id`."""
        ...

    async def reset_extraction(self, message_ids: Sequence[int]) -> int:
        """Mark these messages pending extraction again; returns how many changed.

        A message already pending, or deleted since it was scanned, is left
        alone and not counted.
        """
        ...


@dataclass(frozen=True, slots=True)
class BackfillReport:
    scanned: int = 0
    matched: int = 0
    reset: int = 0

    @property
    def already_pending(self) -> int:
        return self.matched - self.reset


class DecisionBackfill:
    """Resets the extraction watermark of marker-bearing messages in a window."""

    def __init__(
        self,
        ledger: BackfillLedger,
        markers: re.Pattern[str] = DECISION_MARKERS,
        page: int = SCAN_PAGE,
    ) -> None:
        self._ledger = ledger
        self._markers = markers
        self._page = page

    async def run(self, since: datetime, channel_ids: frozenset[int]) -> BackfillReport:
        """Scan the window page by page and reset each page's matches.

        An empty scope resets nothing, rather than everything: indexing is
        opt-in, and the worker would not read an unscoped message anyway.
        """
        report = BackfillReport()
        if not channel_ids:
            return report
        after = 0
        while page := await self._ledger.live_messages_since(
            since, channel_ids, after, self._page
        ):
            matched = [m.platform_message_id for m in page if self._markers.search(m.content)]
            reset = await self._ledger.reset_extraction(matched) if matched else 0
            report = BackfillReport(
                scanned=report.scanned + len(page),
                matched=report.matched + len(matched),
                reset=report.reset + reset,
            )
            after = page[-1].platform_message_id
        return report
