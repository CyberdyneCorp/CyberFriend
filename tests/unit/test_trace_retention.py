"""Traces are kept `TRACE_RETENTION_DAYS`, and only ours are ever deleted.

Self-hosted Langfuse has no retention, so ingest sweeps: the index marks what
it exported before the cutoff, and a Langfuse search marks what the index never
recorded. The search is the dangerous half -- it deletes by query in a project
another application may share -- so every row it returns is re-checked for our
environment, our names and the cutoff, whatever the server claims to filter.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from chatmemory.adapters.store.trace_postgres import PostgresTraceIndex
from chatmemory.adapters.tracing.langfuse import (
    APP_TAG,
    LEGACY_UNTIL,
    LangfuseTraceDeleter,
    LangfuseTraceFinder,
)
from chatmemory.app.reasoning.tracing import (
    RetentionSweep,
    TraceRetention,
    TraceWithdrawal,
)
from chatmemory.composition import build_trace_retention, build_trace_withdrawal
from chatmemory.config import Settings
from chatmemory.entrypoints.ingest import trace_retention_loop, trace_withdrawal_pass
from chatmemory.health import HealthState

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
    user_id: str = "4242",
) -> dict[str, Any]:
    return {
        "id": trace_id,
        "name": name,
        "environment": environment,
        "tags": list(tags),
        "timestamp": timestamp,
        "userId": user_id,
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


# After the tag shipped, so a cutoff past `LEGACY_UNTIL` can be tested.
LATE_CUTOFF = LEGACY_UNTIL + timedelta(days=60)


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def test_another_apps_legacy_named_traces_in_our_environment_survive() -> None:
    """`fixed` and `loop` are generic names, and the sweep has no asker to
    narrow by: an untagged one is ours only from before the tag, naming a
    Discord user, and never when another application's tag is on it."""
    before_tag = _stamp(LEGACY_UNTIL - timedelta(days=30))
    after_tag = _stamp(LEGACY_UNTIL + timedelta(days=30))
    langfuse = IgnoringLangfuse([
        _row("ours-legacy", "loop", timestamp=before_tag),
        _row("ours-tagged-loop", "loop", tags=[APP_TAG], timestamp=after_tag),
        _row("other-app-loop", "loop", tags=["app:other"], timestamp=before_tag),
        _row("other-app-fixed-after-tag", "fixed", timestamp=after_tag),
        _row("other-app-fixed-no-user", "fixed", timestamp=before_tag, user_id=""),
        _row("other-app-fixed-named-user", "fixed", timestamp=before_tag,
             user_id="alice@example.com"),
    ])

    found = await _finder(langfuse).find_traces_before(LATE_CUTOFF)

    assert sorted(found or []) == ["ours-legacy", "ours-tagged-loop"]


async def test_the_legacy_name_queries_stop_at_the_tag() -> None:
    langfuse = IgnoringLangfuse([])

    await _finder(langfuse).find_traces_before(LATE_CUTOFF)

    bounds = {
        r.url.params.get("tags") or r.url.params["name"]: r.url.params["toTimestamp"]
        for r in langfuse.requests
    }
    assert bounds == {
        APP_TAG: _stamp(LATE_CUTOFF),
        "fixed": _stamp(LEGACY_UNTIL),
        "loop": _stamp(LEGACY_UNTIL),
    }


async def test_the_opt_out_search_skips_another_apps_legacy_named_trace() -> None:
    langfuse = IgnoringLangfuse([
        _row("ours", "fixed"),
        _row("other-app", "fixed", tags=["app:other"]),
    ])

    assert await _finder(langfuse).find_traces_by_user(4242) == ["ours"]


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


def test_nothing_is_deleted_when_tracing_is_disabled_with_keys_still_set() -> None:
    """Turning tracing off must stop the deletion sweeps too, not only exports."""
    settings = Settings(**(CONFIGURED | {"tracing_enabled": False}))  # type: ignore[arg-type]

    assert build_trace_retention(settings, object()) is None  # type: ignore[arg-type]
    assert build_trace_withdrawal(settings, object()) is None  # type: ignore[arg-type]


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

    def called_in(name: str) -> set[str]:
        [fn] = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == name
        ]
        return {
            n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    assert "start_trace_sweeps" in called_in("main")
    assert "trace_retention_loop" in called_in("start_trace_sweeps"), (
        "without it traces are kept forever"
    )


class FlakyRetention:
    """Fails its first sweep, then succeeds; the second success ends the test."""

    def __init__(self) -> None:
        self.calls = 0

    async def sweep(self, now: datetime) -> RetentionSweep:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("database unreachable")
        if self.calls == 3:
            raise asyncio.CancelledError
        return RetentionSweep(expired=2, found=1)


async def test_a_failed_sweep_does_not_end_the_retention_loop() -> None:
    """The loop runs in ingest's TaskGroup: an escaping error would cancel
    the whole process, not just skip a day."""
    retention, state = FlakyRetention(), HealthState()

    with pytest.raises(asyncio.CancelledError):
        await trace_retention_loop(retention, state, interval=0)  # type: ignore[arg-type]

    assert retention.calls == 3
    assert state.details["trace_retention"]["expired"] == 2


# --- draining the shared deletion queue --------------------------------------


class QueueIndex:
    """A pending queue in FIFO order, as `trace_export` orders it."""

    def __init__(self, pending: Sequence[str]) -> None:
        self.pending = list(pending)

    async def open_asker_searches(self, limit: int) -> list[int]:
        return []

    async def pending_deletions(self, limit: int) -> list[str]:
        return self.pending[:limit]

    async def confirm_deleted(self, trace_ids: Sequence[str]) -> None:
        self.pending = [t for t in self.pending if t not in set(trace_ids)]


async def test_an_opt_out_behind_a_retention_backlog_is_deleted_in_the_same_pass() -> None:
    """A retention backlog far larger than one batch must not hold back an
    opt-out marked after it until the backlog drains batch by batch."""
    deleted: list[str] = []

    def langfuse(request: httpx.Request) -> httpx.Response:
        deleted.extend(json.loads(request.content)["traceIds"])
        return httpx.Response(200, json={})

    index = QueueIndex([f"expired-{n:04d}" for n in range(250)] + ["opted-out"])
    deleter = LangfuseTraceDeleter(
        "http://langfuse.invalid", "pk", "sk", transport=httpx.MockTransport(langfuse)
    )
    withdrawal = TraceWithdrawal(index, deleter)  # type: ignore[arg-type]
    state = HealthState()

    await trace_withdrawal_pass(withdrawal, state)

    assert "opted-out" in deleted
    assert index.pending == []
    assert state.details["trace_withdrawal"]["withdrawn"] == 251


async def test_a_refused_deletion_stops_the_drain() -> None:
    index = QueueIndex(["a", "b", "c"])
    deleter = LangfuseTraceDeleter(
        "http://langfuse.invalid", "pk", "sk",
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )

    assert await TraceWithdrawal(index, deleter).drain_pending(batch=1) == 0  # type: ignore[arg-type]
    assert index.pending == ["a", "b", "c"]
