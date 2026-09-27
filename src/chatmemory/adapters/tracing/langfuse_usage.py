"""`UsageSource` over Langfuse's v1 read APIs, scoped to this app and environment.

Aggregates come from `GET /api/public/metrics` (the v1 metrics API; the v2 one
needs Langfuse v4's data model), questions from `GET /api/public/traces`.
Every query filters on our environment and on `APP_TAG`, and every metrics
query groups by `userId` and by day, so the application can drop the people
it must not show before anything is summed.

*   runs: traces view, `[userId, name]`, count;
*   model usage: observations view, generations, `[userId, traceName,
    providedModelName]`, input and output tokens and total cost;
*   tool calls: observations view, spans, `[userId, traceName, name]`, count.

A result that reaches `ROW_LIMIT` rows may be truncated, so its window is split
in halves until each half fits. A single day that still reaches it is
`UsageUnavailable`: partial counts are never shown as current.

The traces whose deletion was requested go in the query itself, as a `none of`
filter inside the `query` GET parameter. More than `MAX_EXCLUDED_TRACES` of
them would make a request line longer than servers accept (Node's default
header limit is 16 KiB), so above that the store is `UsageUnavailable` on
purpose, until the deletions are confirmed, rather than failing at random.

The trace list is re-checked row by row -- environment, tag, trace name and
asker -- so a server that ignored a filter still cannot put another
application's or another person's trace in front of an admin. Of a trace only
the question, time, feature, tools, tokens and cost are kept: the answer, the
evidence references and the decision trail are dropped here, server-side.

Any failure -- unreachable, refused, timed out, malformed -- is
`UsageUnavailable`. The keys never leave this process.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog

from chatmemory.adapters.tracing.langfuse import APP_TAG, TRACE_NAMES
from chatmemory.ports.usage import (
    ModelCall,
    QuestionPage,
    ToolCall,
    TraceCount,
    TracedQuestion,
    UsageRows,
    UsageUnavailable,
    UsageWindow,
)

log = structlog.get_logger()

DEFAULT_TIMEOUT = 10.0
ROW_LIMIT = 1000
PAGE_SIZE = 50
MAX_QUESTION_CHARS = 2000
"""A question longer than this is cut: the view is for reading, not for export."""
TOOL_TAG = "tool:"
MAX_EXCLUDED_TRACES = 250
"""About 11 KiB of URL-encoded ids: a metrics request stays under 16 KiB."""

Row = dict[str, Any]


class LangfuseUsageSource:
    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        environment: str = "production",
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = host.rstrip("/") + "/api/public"
        self._auth = (public_key, secret_key)
        self._environment = environment
        self._timeout = timeout
        self._transport = transport

    # --- aggregates ------------------------------------------------------

    async def aggregate(
        self, window: UsageWindow, excluded_trace_ids: Collection[str]
    ) -> UsageRows:
        excluded = _excluded(excluded_trace_ids)
        async with self._client() as client:
            traces, models, tools = await asyncio.gather(
                self._split(client, window, lambda w: self._runs_query(w, excluded)),
                self._split(client, window, lambda w: self._models_query(w, excluded)),
                self._split(client, window, lambda w: self._tools_query(w, excluded)),
            )
        return UsageRows(
            traces=tuple(_run(r) for r in traces if _asker(r)),
            models=tuple(_model(r) for r in models if _asker(r)),
            tools=tuple(_tool(r) for r in tools if _asker(r)),
        )

    async def count(
        self,
        user_id: str,
        since: datetime,
        until: datetime,
        excluded_trace_ids: Collection[str],
    ) -> int:
        query = {
            "view": "traces",
            "dimensions": [],
            "metrics": [{"measure": "count", "aggregation": "count"}],
            "filters": [
                *self._scope(),
                _equals("userId", user_id),
                *_none_of("id", _excluded(excluded_trace_ids)),
            ],
            "fromTimestamp": _stamp(since),
            "toTimestamp": _stamp(until),
        }
        async with self._client() as client:
            rows = await self._metrics(client, query)
        return sum(_int(r.get("count_count")) for r in rows)

    async def _split(
        self,
        client: httpx.AsyncClient,
        window: UsageWindow,
        build: Callable[[UsageWindow], Row],
    ) -> list[Row]:
        rows = await self._metrics(client, build(window))
        if len(rows) < ROW_LIMIT:
            return rows
        if window.days <= 1:
            log.warning("usage.row_limit_reached", day=window.start.date().isoformat())
            raise UsageUnavailable("one day of usage is more than a metrics answer holds")
        middle = window.start + timedelta(days=window.days // 2)
        first = await self._split(client, UsageWindow(window.start, middle), build)
        second = await self._split(client, UsageWindow(middle, window.end), build)
        return first + second

    def _scope(self) -> list[Row]:
        return [
            _equals("environment", self._environment),
            {"column": "tags", "operator": "any of", "value": [APP_TAG], "type": "arrayOptions"},
        ]

    def _daily(
        self,
        view: str,
        window: UsageWindow,
        dimensions: Sequence[str],
        metrics: Sequence[tuple[str, str]],
        filters: Sequence[Row],
    ) -> Row:
        return {
            "view": view,
            "dimensions": [{"field": d} for d in dimensions],
            "metrics": [{"measure": m, "aggregation": a} for m, a in metrics],
            "filters": [*self._scope(), *filters],
            "timeDimension": {"granularity": "day"},
            "fromTimestamp": _stamp(window.start),
            "toTimestamp": _stamp(window.end),
            "config": {"row_limit": ROW_LIMIT},
        }

    def _runs_query(self, window: UsageWindow, excluded: list[str]) -> Row:
        return self._daily(
            "traces", window, ["userId", "name"], [("count", "count")], _none_of("id", excluded)
        )

    def _models_query(self, window: UsageWindow, excluded: list[str]) -> Row:
        return self._daily(
            "observations",
            window,
            ["userId", "traceName", "providedModelName"],
            [("inputTokens", "sum"), ("outputTokens", "sum"), ("totalCost", "sum")],
            [_equals("type", "GENERATION"), *_none_of("traceId", excluded)],
        )

    def _tools_query(self, window: UsageWindow, excluded: list[str]) -> Row:
        return self._daily(
            "observations",
            window,
            ["userId", "traceName", "name"],
            [("count", "count")],
            [_equals("type", "SPAN"), *_none_of("traceId", excluded)],
        )

    async def _metrics(self, client: httpx.AsyncClient, query: Row) -> list[Row]:
        body = await self._get(client, "/metrics", {"query": json.dumps(query)})
        data = body.get("data")
        if not isinstance(data, list):
            raise UsageUnavailable("the metrics answer carried no data")
        return [r for r in data if isinstance(r, dict)]

    # --- questions -------------------------------------------------------

    async def questions(
        self, user_id: str, since: datetime, until: datetime, page: int
    ) -> QuestionPage:
        params: dict[str, str | int] = {
            "userId": user_id,
            "environment": self._environment,
            "tags": APP_TAG,
            "fields": "core,io,metrics",
            "fromTimestamp": _stamp(since),
            "toTimestamp": _stamp(until),
            "orderBy": "timestamp.desc",
            "limit": PAGE_SIZE,
            "page": page,
        }
        async with self._client() as client:
            body = await self._get(client, "/traces", params)
        rows = body.get("data")
        if not isinstance(rows, list):
            raise UsageUnavailable("the traces answer carried no data")
        meta = body.get("meta") or {}
        kept = tuple(_question(r) for r in rows if self._is_ours(r, user_id))
        return QuestionPage(kept, page, _int(meta.get("totalPages")))

    def _is_ours(self, row: Any, user_id: str) -> bool:
        return (
            isinstance(row, dict)
            and row.get("environment") == self._environment
            and APP_TAG in (row.get("tags") or ())
            and row.get("name") in TRACE_NAMES
            and str(row.get("userId")) == user_id
            and _timestamp(row) is not None
        )

    # --- transport -------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport, auth=self._auth
        )

    async def _get(
        self, client: httpx.AsyncClient, path: str, params: dict[str, str | int]
    ) -> Row:
        try:
            response = await client.get(self._base + path, params=params)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("usage.langfuse_unreadable", path=path, error=type(exc).__name__)
            raise UsageUnavailable("the trace store could not be read") from exc
        if not isinstance(body, dict):
            raise UsageUnavailable("the trace store answered with something unexpected")
        return body


# --- rows ----------------------------------------------------------------


def _equals(column: str, value: str) -> Row:
    return {"column": column, "operator": "=", "value": value, "type": "string"}


def _excluded(trace_ids: Collection[str]) -> list[str]:
    if len(trace_ids) > MAX_EXCLUDED_TRACES:
        log.warning("usage.too_many_pending_deletions", count=len(trace_ids))
        raise UsageUnavailable("too many traces are awaiting deletion to leave them out")
    return sorted(trace_ids)


def _none_of(column: str, values: list[str]) -> list[Row]:
    if not values:
        return []
    return [{"column": column, "operator": "none of", "value": values, "type": "stringOptions"}]


def _asker(row: Row) -> bool:
    return bool(str(row.get("userId") or "").strip())


def _day(row: Row) -> date:
    raw = str(row.get("time_dimension") or "")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise UsageUnavailable("a metrics row carried no day") from exc


def _run(row: Row) -> TraceCount:
    return TraceCount(
        user_id=str(row["userId"]),
        day=_day(row),
        feature=str(row.get("name") or ""),
        count=_int(row.get("count_count")),
    )


def _model(row: Row) -> ModelCall:
    return ModelCall(
        user_id=str(row["userId"]),
        day=_day(row),
        feature=str(row.get("traceName") or ""),
        model=str(row.get("providedModelName") or "unknown"),
        input_tokens=_int(row.get("sum_inputTokens")),
        output_tokens=_int(row.get("sum_outputTokens")),
        cost=_float(row.get("sum_totalCost")) or 0.0,
    )


def _tool(row: Row) -> ToolCall:
    return ToolCall(
        user_id=str(row["userId"]),
        day=_day(row),
        feature=str(row.get("traceName") or ""),
        tool=str(row.get("name") or ""),
        calls=_int(row.get("count_count")),
    )


def _question(row: Row) -> TracedQuestion:
    """The asker's words and metadata; the rest of the trace is dropped here."""
    given = row.get("input")
    text = given.get("question") if isinstance(given, dict) else None
    metadata = row.get("metadata")
    usage = metadata if isinstance(metadata, dict) else {}
    tags = row.get("tags") or ()
    stamp = _timestamp(row)
    assert stamp is not None  # checked by `_is_ours`
    return TracedQuestion(
        trace_id=str(row.get("id") or ""),
        timestamp=stamp,
        feature=str(row.get("name") or ""),
        tools=tuple(sorted(str(t)[len(TOOL_TAG):] for t in tags if str(t).startswith(TOOL_TAG))),
        question=str(text or "")[:MAX_QUESTION_CHARS],
        input_tokens=_int(usage.get("prompt_tokens")),
        output_tokens=_int(usage.get("completion_tokens")),
        cost=_float(row.get("totalCost")),
    )


def _timestamp(row: Row) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class UnconfiguredUsageSource:
    """The trace store when no Langfuse keys are configured: always unavailable.

    The usage screen then says usage is unavailable, which is true, rather
    than showing zeros, which would not be.
    """

    async def aggregate(
        self, window: UsageWindow, excluded_trace_ids: Collection[str]
    ) -> UsageRows:
        raise UsageUnavailable("no trace store is configured")

    async def count(
        self,
        user_id: str,
        since: datetime,
        until: datetime,
        excluded_trace_ids: Collection[str],
    ) -> int:
        raise UsageUnavailable("no trace store is configured")

    async def questions(
        self, user_id: str, since: datetime, until: datetime, page: int
    ) -> QuestionPage:
        raise UsageUnavailable("no trace store is configured")
