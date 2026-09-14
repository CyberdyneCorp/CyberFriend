"""Ports the ask pipeline is written against.

Note the same asymmetry as the corpus store: writes take what they need, but
every method that *returns* an ask takes a `Viewer` as a required positional
argument. An ask is a summary of a conversation, and summaries travel further
than quotes do, so an unfiltered read must be unrepresentable here too.

There is deliberately no method for reading someone else's obligations. The
person whose asks are returned is taken from the viewer inside the adapter, so
"show me what was asked of Hezron" cannot be expressed at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from chatmemory.app.asks.model import (
    Ask,
    AskCandidate,
    Correction,
    CorrectionOutcome,
    ExtractedAsk,
    ObligationRequest,
    ReportedAsk,
    StateRefresh,
)
from chatmemory.domain.identity import PersonRef, Viewer


class AskExtractor(Protocol):
    """A model that reads one candidate message and reports what it asks."""

    async def extract(self, candidate: AskCandidate) -> Sequence[ExtractedAsk]: ...


class AskStore(Protocol):
    async def record_asks(self, source_message_id: int, asks: Sequence[Ask]) -> int:
        """Replace the asks extracted from one message. Idempotent.

        Scoped to a single source message so reprocessing a window cannot
        duplicate obligations, and so a re-run that no longer finds an ask
        withdraws it -- except where the addressee has corrected it, which
        outranks anything extraction concludes.
        """
        ...

    async def obligations(
        self, viewer: Viewer, request: ObligationRequest
    ) -> Sequence[ReportedAsk]:
        """The viewer's own asks, from channels they may read."""
        ...

    async def count_outstanding(self, viewer: Viewer, request: ObligationRequest) -> int:
        """How many the viewer has. Asks they may not read are not counted."""
        ...

    async def apply_correction(
        self, viewer: Viewer, correction: Correction
    ) -> CorrectionOutcome: ...

    async def record_reaction(
        self, source_message_id: int, person: PersonRef, emoji: str, at: datetime
    ) -> None:
        """Record an observed reaction on a message an ask came from."""
        ...

    async def refresh_state(
        self, now: datetime, stale_after: timedelta, acknowledging: frozenset[str]
    ) -> StateRefresh:
        """Apply every observable state transition. Makes no model call."""
        ...
