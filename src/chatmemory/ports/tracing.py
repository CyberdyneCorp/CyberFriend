"""The record of what was exported, so that deleting it can follow.

Separate from the tracer itself because the two ends run in different
processes: the bot exports a trace and writes the mapping, `ingest` reads it
when a message is deleted. Neither holds the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class TraceIndex(Protocol):
    async def record_export(self, trace_id: str, message_ids: Sequence[int]) -> None:
        """Note that `trace_id` quotes these messages.

        Must not raise: losing the mapping costs a trace that outlives its
        message, and failing here would cost the requester their answer.
        """
        ...

    async def request_deletion_for_message(self, message_id: int) -> Sequence[str]:
        """Mark every trace quoting `message_id` for deletion; return their ids."""
        ...

    async def confirm_deleted(self, trace_ids: Sequence[str]) -> None:
        """Record that the destination accepted the deletion of these traces."""
        ...

    async def pending_deletions(self, limit: int) -> Sequence[str]:
        """Traces asked to be deleted that the destination has not confirmed."""
        ...


class TraceDeleter(Protocol):
    async def delete_traces(self, trace_ids: Sequence[str]) -> bool:
        """Delete these traces at the destination. False if it could not be done.

        Returns rather than raises, because the caller is a deletion path: a
        person removing a message must not have that blocked by a trace store
        being down.
        """
        ...
