"""Traces are kept `TRACE_RETENTION_DAYS`, and only ours are ever deleted.

Self-hosted Langfuse has no retention, so ingest sweeps: the index marks what
it exported before the cutoff, and a Langfuse search marks what the index never
recorded. The search is the dangerous half -- it deletes by query in a project
another application may share -- so every row it returns is re-checked for our
environment, our names and the cutoff, whatever the server claims to filter.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from chatmemory.adapters.store.trace_postgres import PostgresTraceIndex
from chatmemory.adapters.tracing.langfuse import APP_TAG, LangfuseTraceFinder
from chatmemory.app.reasoning.tracing import RetentionSweep, TraceRetention
from chatmemory.composition import build_trace_retention
from chatmemory.config import Settings

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"
INGEST = SRC / "entrypoints" / "ingest.py"

BASE = {
    "discord_token": "x",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "llm_api_key": "k",
}
CONFIGURED = BASE | {
    "tracing_enabled": True,
    "langfuse_host": "https://langfuse.example",
    "langfuse_public_key": "pk",
    "langfuse_secret_key": "sk",
}

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=90)
OLD = (CUTOFF - timedelta(days=1)).isoformat().replace("+00:00", "Z")
RECENT = (CUTOFF + timedelta(days=1)).isoformat().replace("+00:00", "Z")


def _row(
    trace_id: str,
    name: str,
    *,
    environment: str = "production",
    tags: Sequence[str] = (),
    timestamp: str = OLD,
) -> dict[str, Any]:
    return {
        "id": trace_id,
        "name": name,
        "environment": environment,
        "tags": list(tags),
        "timestamp": timestamp,
    }


class IgnoringLangfuse:
    """A Langfuse that ignores every filter and lists everything it holds."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200, json={"data": self.rows, "meta": {"page": 1, "totalPages": 1}}
        )


def _finder(handler: object, environment: str = "production") -> LangfuseTraceFinder:
    return LangfuseTraceFinder(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        environment=environment,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


# --- the Langfuse backstop -----------------------------------------------


async def test_the_backstop_returns_only_our_old_traces() -> None:
    langfuse = IgnoringLangfuse([
        _row("tagged-feature", "time", tags=[APP_TAG]),
        _row("legacy-untagged", "loop"),
        _row("foreign-env", "fixed", environment="staging", tags=[APP_TAG]),
        _row("foreign-name", "checkout", tags=[APP_TAG]),
        # A feature id is a generic word: without our tag it is not ours.
        _row("untagged-feature", "time"),
        _row("recent", "fixed", tags=[APP_TAG], timestamp=RECENT),
        _row("no-timestamp", "fixed", timestamp=""),
    ])

    found = await _finder(langfuse).find_traces_before(CUTOFF)

    assert sorted(found or []) == ["legacy-untagged", "tagged-feature"]


async def test_the_backstop_asks_for_our_environment_tag_and_legacy_names() -> None:
    langfuse = IgnoringLangfuse([])

    await _finder(langfuse, environment="staging").find_traces_before(CUTOFF)

    params = [dict(r.url.params) for r in langfuse.requests]
    assert all(p["environment"] == "staging" for p in params)
    assert all(p["toTimestamp"] == "2026-06-27T12:00:00Z" for p in params)
    assert [p.get("tags") or p.get("name") for p in params] == [APP_TAG, "fixed", "loop"]


async def test_the_backstop_reads_every_page() -> None:
    def paged(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("tags") != APP_TAG:
            return httpx.Response(200, json={"data": [], "meta": {"totalPages": 0}})
        page = int(request.url.params["page"])
        rows = [_row(f"t{page}", "fixed", tags=[APP_TAG])]
        return httpx.Response(200, json={"data": rows, "meta": {"totalPages": 2}})

    assert await _finder(paged).find_traces_before(CUTOFF) == ["t1", "t2"]


async def test_an_unreadable_langfuse_is_none_not_nothing() -> None:
    """None keeps the sweep honest: an empty list would read as "nothing old"."""
    found = await _finder(lambda r: httpx.Response(500)).find_traces_before(CUTOFF)
    assert found is None


# --- the sweep ---------------------------------------------------------------


class FakeIndex:
    def __init__(self, expired: Sequence[str]) -> None:
        self._expired = list(expired)
        self.cutoffs: list[datetime] = []
        self.recorded: list[list[str]] = []

    async def request_deletion_before(self, cutoff: datetime) -> Sequence[str]:
        self.cutoffs.append(cutoff)
        return self._expired

    async def record_expired_traces(self, trace_ids: Sequence[str]) -> None:
        self.recorded.append(list(trace_ids))


class FakeFinder:
    def __init__(self, found: Sequence[str] | None) -> None:
        self._found = found
        self.cutoffs: list[datetime] = []

    async def find_traces_before(self, cutoff: datetime) -> Sequence[str] | None:
        self.cutoffs.append(cutoff)
        return self._found


async def test_the_sweep_marks_indexed_and_found_traces_before_the_cutoff() -> None:
    index, finder = FakeIndex(["a", "b"]), FakeFinder(["c"])
    retention = TraceRetention(index, finder, timedelta(days=90))  # type: ignore[arg-type]

    assert await retention.sweep(NOW) == RetentionSweep(expired=2, found=1)
    assert index.cutoffs == finder.cutoffs == [CUTOFF]
    assert index.recorded == [["c"]]


async def test_an_unreadable_langfuse_still_expires_the_index() -> None:
    index, finder = FakeIndex(["a"]), FakeFinder(None)
    retention = TraceRetention(index, finder, timedelta(days=90))  # type: ignore[arg-type]

    assert await retention.sweep(NOW) == RetentionSweep(expired=1, found=None)
    assert index.recorded == []


# --- settings and wiring ---------------------------------------------------


def test_traces_are_kept_ninety_days_by_default() -> None:
    assert Settings(**BASE).trace_retention_days == 90  # type: ignore[arg-type]


def test_the_retention_period_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACE_RETENTION_DAYS", "30")
    assert Settings(**BASE).trace_retention_days == 30  # type: ignore[arg-type]


def test_a_zero_retention_period_is_refused() -> None:
    """Zero would delete every trace on the next sweep, including this minute's."""
    with pytest.raises(ValidationError):
        Settings(**BASE, trace_retention_days=0)  # type: ignore[arg-type]


def test_build_trace_retention_is_off_without_tracing() -> None:
    assert build_trace_retention(Settings(**BASE), object()) is None  # type: ignore[arg-type]


def test_build_trace_retention_searches_our_environment_over_the_transport() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    settings = Settings(  # type: ignore[arg-type]
        **CONFIGURED, langfuse_environment="staging", trace_retention_days=30
    )

    retention = build_trace_retention(settings, object(), transport)  # type: ignore[arg-type]

    assert isinstance(retention, TraceRetention)
    assert isinstance(retention._index, PostgresTraceIndex)  # noqa: SLF001
    finder = retention._finder  # noqa: SLF001
    assert isinstance(finder, LangfuseTraceFinder)
    assert finder._environment == "staging"  # noqa: SLF001
    assert finder._transport is transport  # noqa: SLF001
    assert retention._retention == timedelta(days=30)  # noqa: SLF001


def test_ingest_starts_the_retention_sweep() -> None:
    tree = ast.parse(INGEST.read_text())
    [main] = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == "main"
    ]
    called = {
        n.func.id for n in ast.walk(main)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "trace_retention_loop" in called, "without it traces are kept forever"
