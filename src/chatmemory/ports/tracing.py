"""The record of what was exported, so that deleting it can follow.

Separate from the tracer itself because the two ends run in different
processes: the bot exports a trace and writes the mapping, `ingest` reads it
when a message is deleted. Neither holds the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from chatmemory.domain.identity import PersonRef


class TraceIndex(Protocol):
    async def record_export(
        self,
        trace_id: str,
        message_ids: Sequence[int],
        asker: PersonRef | None = None,
    ) -> None:
        """Note that `trace_id` quotes these messages and was asked by `asker`.

        A trace whose asker has already opted out is recorded as pending
        deletion: the opt-out may have landed while the run was answering.

        Must not raise: losing the mapping costs a trace that outlives its
        message, and failing here would cost the requester their answer.
        """
        ...

    async def request_deletion_for_message(self, message_id: int) -> Sequence[str]:
        """Mark every trace quoting `message_id` for deletion; return their ids."""
        ...

    async def request_deletion_for_asker(
        self, platform_user_ids: Sequence[int]
    ) -> Sequence[str]:
        """Mark every trace asked by one of these platform ids; return their ids."""
        ...

    async def open_asker_searches(self, limit: int) -> Sequence[int]:
        """Platform ids whose traces still have to be looked up at the destination."""
        ...

    async def record_found_traces(
        self, platform_user_id: int, trace_ids: Sequence[str], started: datetime
    ) -> None:
        """Mark traces the destination holds for this asker, and close the search.

        A search re-requested after `started` stays open for another pass.
        """
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


class TraceFinder(Protocol):
    async def find_traces_by_user(self, platform_user_id: int) -> Sequence[str] | None:
        """Every trace of this application the destination holds for this asker.

        None if the destination could not be read, so the search stays open
        and is retried. Never another application's or environment's traces.
        """
        ...
