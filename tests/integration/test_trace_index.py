"""The durable half of "deleting a message deletes the traces quoting it".

The interesting statement is `REQUEST_DELETION`: it marks and returns in one
UPDATE, and it is idempotent. Both matter. Two statements would let a
concurrent sweep claim the same rows, and a mark that moved on re-request
would make a repeated deletion look like new work forever.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.trace_postgres import PostgresTraceIndex

pytestmark = pytest.mark.asyncio


async def test_an_exported_trace_is_found_by_every_message_it_quotes(
    clean: AsyncEngine,
) -> None:
    index = PostgresTraceIndex(clean)
    await index.record_export("trace-a", [101, 102])

    assert await index.request_deletion_for_message(101) == ["trace-a"]
    # The same trace is reachable from its other message: a second deletion
    # finds it already marked and returns it again rather than losing it.
    assert await index.request_deletion_for_message(102) == ["trace-a"]


async def test_a_message_nothing_quoted_marks_nothing(clean: AsyncEngine) -> None:
    index = PostgresTraceIndex(clean)
    await index.record_export("trace-a", [101])
    assert await index.request_deletion_for_message(999) == []


async def test_a_marked_trace_stays_pending_until_the_destination_confirms(
    clean: AsyncEngine,
) -> None:
    index = PostgresTraceIndex(clean)
    await index.record_export("trace-a", [101])
    await index.request_deletion_for_message(101)

    assert await index.pending_deletions(10) == ["trace-a"]

    await index.confirm_deleted(["trace-a"])
    assert await index.pending_deletions(10) == []


async def test_a_confirmed_deletion_is_not_reopened_by_a_later_request(
    clean: AsyncEngine,
) -> None:
    """A message can be tombstoned twice -- a delete and then a reconciliation
    pass noticing the same absence. The second must not resurrect the work."""
    index = PostgresTraceIndex(clean)
    await index.record_export("trace-a", [101])
    await index.request_deletion_for_message(101)
    await index.confirm_deleted(["trace-a"])

    assert await index.request_deletion_for_message(101) == []
    assert await index.pending_deletions(10) == []


async def test_recording_the_same_export_twice_is_harmless(clean: AsyncEngine) -> None:
    index = PostgresTraceIndex(clean)
    await index.record_export("trace-a", [101, 101, 102])
    await index.record_export("trace-a", [101, 102])

    async with clean.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM trace_export_message WHERE trace_id = 'trace-a'")
        )
        assert rows.scalar_one() == 2


async def test_a_trace_that_quotes_nothing_is_still_recorded(
    clean: AsyncEngine,
) -> None:
    """An answer from the web quotes no corpus message. The trace exists and
    must be countable; it simply has no tombstone coming for it."""
    index = PostgresTraceIndex(clean)
    await index.record_export("trace-web", [])

    async with clean.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM trace_export WHERE trace_id = 'trace-web'")
        )
        assert rows.scalar_one() == 1


async def test_pending_deletions_are_returned_oldest_first(clean: AsyncEngine) -> None:
    index = PostgresTraceIndex(clean)
    await index.record_export("trace-a", [101])
    await index.record_export("trace-b", [102])
    await index.request_deletion_for_message(101)
    await index.request_deletion_for_message(102)

    assert await index.pending_deletions(10) == ["trace-a", "trace-b"]
    assert await index.pending_deletions(1) == ["trace-a"]
