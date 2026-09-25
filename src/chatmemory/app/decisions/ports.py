"""The ports decisions are written and read through.

The read takes a `Viewer` as a required positional argument, for the reason
`app/asks/ports.py` gives: a decision is a summary of a conversation, and an
unfiltered read of summaries must be unrepresentable. It is its own protocol
because the two halves live in different processes: ingest writes, the bot
reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from chatmemory.app.decisions.model import Decision, DecisionRequest, ReportedDecision
from chatmemory.domain.identity import Viewer


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

    async def withdraw_message(self, message_id: int) -> int:
        """Remove every decision stated in or resting on a deleted message.

        A user's deletion is a tombstone, so the source's cascade never fires,
        and it does not bump the extraction revision, so no re-run prunes
        either. The summary may restate the retracted words, so it goes now.
        """
        ...


class DecisionSearch(Protocol):
    async def search(
        self,
        viewer: Viewer,
        request: DecisionRequest,
        query_embedding: Sequence[float] | None,
    ) -> Sequence[ReportedDecision]:
        """The decisions the viewer may read that answer `request`, newest first.

        Scoped in the statement: a decision from a channel outside the viewer's
        set, or resting on a message that is deleted or outside that set, is
        never returned. With a topic, only decisions that clear
        `request.min_similarity` (or match its words, when a decision has no
        vector) are returned; `query_embedding` is None when there is no topic.
        """
        ...
