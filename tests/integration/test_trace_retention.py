"""The retention sweep against the real index, deleting through a fake Langfuse.

Rows past the cutoff become pending and nothing else does; a trace Langfuse
holds without an index row is still deleted; another environment's and another
application's old traces -- legacy-named ones in our environment included --
are never asked to be deleted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.trace_postgres import PostgresTraceIndex
from chatmemory.adapters.tracing.langfuse import (
    APP_TAG,
    LangfuseTraceDeleter,
    LangfuseTraceFinder,
)
from chatmemory.app.reasoning.tracing import TraceRetention, TraceWithdrawal

pytestmark = pytest.mark.asyncio

NOW = datetime.now(UTC)
RETENTION = timedelta(days=90)
HOST = "http://langfuse.invalid"


async def _export(engine: AsyncEngine, trace_id: str, age: timedelta) -> None:
    await PostgresTraceIndex(engine).record_export(trace_id, [101])
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE trace_export SET created_at = :at WHERE trace_id = :id"),
            {"at": NOW - age, "id": trace_id},
        )


async def test_only_rows_past_the_cutoff_become_pending(clean: AsyncEngine) -> None:
    index = PostgresTraceIndex(clean)
    await _export(clean, "old", timedelta(days=91))
    await _export(clean, "recent", timedelta(days=89))

    assert await index.request_deletion_before(NOW - RETENTION) == ["old"]
    assert await index.pending_deletions(10) == ["old"]
    # Already pending: a second sweep reports nothing new and loses nothing.
    assert await index.request_deletion_before(NOW - RETENTION) == []
    assert await index.pending_deletions(10) == ["old"]


async def test_a_confirmed_deletion_is_not_reopened_by_the_sweep(clean: AsyncEngine) -> None:
    """Langfuse deletes asynchronously and may still list a deleted trace."""
    index = PostgresTraceIndex(clean)
    await _export(clean, "old", timedelta(days=91))
    await index.request_deletion_before(NOW - RETENTION)
    await index.confirm_deleted(["old"])

    await index.record_expired_traces(["old"])

    assert await index.request_deletion_before(NOW - RETENTION) == []
    assert await index.pending_deletions(10) == []


class FakeLangfuse:
    """Lists what it holds, honouring the environment, tag and name filters."""

    def __init__(self, held: list[dict[str, Any]]) -> None:
        self.held = held
        self.deleted: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            ids = json.loads(request.content)["traceIds"]
            self.deleted.extend(ids)
            self.held = [r for r in self.held if r["id"] not in ids]
            return httpx.Response(200, json={"message": "Traces deleted"})
        params = request.url.params
        rows = [
            r for r in self.held
            if r["environment"] == params["environment"]
            and ("tags" not in params or params["tags"] in r["tags"])
            and ("name" not in params or params["name"] == r["name"])
        ]
        return httpx.Response(200, json={"data": rows, "meta": {"totalPages": 1}})


def _held(trace_id: str, name: str, environment: str = "production",
          tags: tuple[str, ...] = (APP_TAG,), user_id: str = "101") -> dict[str, Any]:
    stamp = (NOW - timedelta(days=120)).isoformat()
    return {"id": trace_id, "name": name, "environment": environment,
            "tags": list(tags), "timestamp": stamp, "userId": user_id}


async def test_the_backstop_deletes_an_unindexed_trace_and_nothing_foreign(
    clean: AsyncEngine,
) -> None:
    await _export(clean, "indexed-old", timedelta(days=91))
    await _export(clean, "indexed-recent", timedelta(days=1))
    langfuse = FakeLangfuse([
        _held("indexed-old", "fixed"),
        _held("unindexed-old", "time"),
        _held("legacy-untagged", "loop", tags=()),
        _held("foreign-env", "fixed", environment="staging"),
        _held("foreign-name", "checkout"),
        # Legacy names in our environment that are not ours: another app's tag,
        # and an untagged one whose user is not a Discord id.
        _held("other-app-loop", "loop", tags=("app:other",)),
        _held("other-app-fixed", "fixed", tags=(), user_id="checkout-bot"),
    ])
    transport = httpx.MockTransport(langfuse)
    index = PostgresTraceIndex(clean)
    finder = LangfuseTraceFinder(HOST, "pk", "sk", transport=transport)
    retention = TraceRetention(index, finder, RETENTION)
    withdrawal = TraceWithdrawal(
        index, LangfuseTraceDeleter(HOST, "pk", "sk", transport=transport), finder
    )

    swept = await retention.sweep(NOW)
    await withdrawal.retry_pending()

    assert swept.expired == 1
    assert sorted(langfuse.deleted) == ["indexed-old", "legacy-untagged", "unindexed-old"]
    assert sorted(r["id"] for r in langfuse.held) == [
        "foreign-env", "foreign-name", "other-app-fixed", "other-app-loop",
    ]
    assert await index.pending_deletions(10) == []
