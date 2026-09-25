"""The port the extraction pass writes decisions through.

There is no read method yet. When one is added it takes a `Viewer` as a
required positional argument, for the reason `app/asks/ports.py` gives: a
decision is a summary of a conversation, and an unfiltered read of summaries
must be unrepresentable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from chatmemory.app.decisions.model import Decision


class DecisionStore(Protocol):
    async def record_decisions(
        self, source_message_id: int, decisions: Sequence[Decision]
    ) -> int:
        """Replace the decisions extracted from one message. Idempotent.

        Scoped to one source message, like `AskStore.record_asks`: a re-run
        upserts the same keys and withdraws what it no longer finds, which is
        how an edit that takes a decision back removes it.
        """
        ...

    async def withdraw(self, source_message_ids: Sequence[int]) -> int:
        """Remove every decision extracted from these messages.

        For messages a pass read and did not send to the model at all: an edit
        that removes the decision marker makes the message stop being a
        candidate, and without this the decision it used to carry would stay.
        """
        ...
