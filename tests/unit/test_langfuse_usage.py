"""The Langfuse usage reader: scoped queries, trimmed questions, unavailable when down."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from chatmemory.adapters.tracing.langfuse import APP_TAG
from chatmemory.adapters.tracing.langfuse_usage import (
    MAX_QUESTION_CHARS,
    ROW_LIMIT,
    LangfuseUsageSource,
    UnconfiguredUsageSource,
)
from chatmemory.app.usage import window_of
from chatmemory.ports.usage import UsageUnavailable

HOST = "https://langfuse.test"
WINDOW = window_of("2026-09-01", "2026-09-04", date(2026, 9, 26))
SINCE = datetime(2026, 9, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 5, tzinfo=UTC)


class FakeMetrics:
    """Answers `/metrics` per view, and `/traces` with `traces`."""

    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.by_view: dict[str, list[dict[str, Any]]] = {}
        self.traces: list[dict[str, Any]] = []
        self.status = 200
        self.row_limit_days: set[int] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "down"})
        if request.url.path.endswith("/metrics"):
            query = json.loads(request.url.params["query"])
            self.queries.append(query)
            return httpx.Response(200, json={"data": self._rows(query)})
        return httpx.Response(
            200, json={"data": self.traces, "meta": {"page": 1, "totalPages": 3}}
        )

    def _rows(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        start = datetime.fromisoformat(query["fromTimestamp"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(query["toTimestamp"].replace("Z", "+00:00"))
        if (end - start).days in self.row_limit_days:
            return [{"userId": "1", "time_dimension": "2026-09-01", "count_count": 1}] * ROW_LIMIT
        kind = query["view"]
        if kind == "observations":
            kind = next(f["value"] for f in query["filters"] if f["column"] == "type")
        return self.by_view.get(kind, [])


def _source(fake: FakeMetrics) -> LangfuseUsageSource:
    return LangfuseUsageSource(
        HOST, "pk", "sk", environment="production", transport=httpx.MockTransport(fake)
    )


async def test_every_query_is_scoped_grouped_by_asker_and_day_and_leaves_out_pending() -> None:
    fake = FakeMetrics()

    await _source(fake).aggregate(WINDOW, {"t-2", "t-1"})

    assert len(fake.queries) == 3
    for query in fake.queries:
        filters = query["filters"]
        assert {"column": "environment", "operator": "=", "value": "production",
                "type": "string"} in filters
        assert {"column": "tags", "operator": "any of", "value": [APP_TAG],
                "type": "arrayOptions"} in filters
        [none_of] = [f for f in filters if f["operator"] == "none of"]
        assert none_of["value"] == ["t-1", "t-2"]
        assert {"field": "userId"} in query["dimensions"]
        assert query["timeDimension"] == {"granularity": "day"}
        assert query["config"]["row_limit"] == ROW_LIMIT
        assert (query["fromTimestamp"], query["toTimestamp"]) == (
            "2026-09-01T00:00:00Z",
            "2026-09-05T00:00:00Z",
        )
    views = sorted(
        (q["view"], tuple(d["field"] for d in q["dimensions"])) for q in fake.queries
    )
    assert views == [
        ("observations", ("userId", "traceName", "name")),
        ("observations", ("userId", "traceName", "providedModelName")),
        ("traces", ("userId", "name")),
    ]
    assert all(r.headers["authorization"].startswith("Basic ") for r in fake.requests)


async def test_metrics_rows_become_usage_rows() -> None:
    fake = FakeMetrics()
    fake.by_view = {
        "traces": [
            {"userId": "42", "name": "market.price", "time_dimension": "2026-09-02T00:00:00.000Z",
             "count_count": "3"},
            {"userId": None, "name": "market.price", "time_dimension": "2026-09-02",
             "count_count": "9"},
        ],
        "GENERATION": [
            {"userId": "42", "traceName": "market.price", "providedModelName": "gpt-5.4-mini",
             "time_dimension": "2026-09-02", "sum_inputTokens": 1200, "sum_outputTokens": "80",
             "sum_totalCost": "0.0021"},
        ],
        "SPAN": [
            {"userId": "42", "traceName": "market.price", "name": "market:price",
             "time_dimension": "2026-09-02", "count_count": 2},
        ],
    }

    rows = await _source(fake).aggregate(WINDOW, set())

    [run] = rows.traces  # the row without an asker is not anybody's
    assert (run.user_id, run.day, run.feature, run.count) == ("42", date(2026, 9, 2),
                                                              "market.price", 3)
    [model] = rows.models
    assert (model.model, model.input_tokens, model.output_tokens) == ("gpt-5.4-mini", 1200, 80)
    assert model.cost == pytest.approx(0.0021)
    [tool] = rows.tools
    assert (tool.tool, tool.calls) == ("market:price", 2)


async def test_a_result_at_the_row_limit_is_split_until_it_fits() -> None:
    fake = FakeMetrics()
    fake.row_limit_days = {4}

    await _source(fake).aggregate(WINDOW, set())

    spans = sorted(
        {(q["fromTimestamp"][:10], q["toTimestamp"][:10]) for q in fake.queries}
    )
    assert spans == [("2026-09-01", "2026-09-03"), ("2026-09-01", "2026-09-05"),
                     ("2026-09-03", "2026-09-05")]


@pytest.mark.parametrize("status", [500, 404, 401])
async def test_a_failing_store_is_unavailable(status: int) -> None:
    fake = FakeMetrics()
    fake.status = status
    with pytest.raises(UsageUnavailable):
        await _source(fake).aggregate(WINDOW, set())
    with pytest.raises(UsageUnavailable):
        await _source(fake).questions("42", SINCE, UNTIL, 1)


async def test_an_unreachable_store_is_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    source = LangfuseUsageSource(HOST, "pk", "sk", transport=httpx.MockTransport(refuse))
    with pytest.raises(UsageUnavailable):
        await source.count("42", SINCE, UNTIL, set())


async def test_no_keys_means_unavailable() -> None:
    with pytest.raises(UsageUnavailable):
        await UnconfiguredUsageSource().aggregate(WINDOW, set())


async def test_the_count_is_one_askers_runs_in_the_range() -> None:
    fake = FakeMetrics()
    fake.by_view = {"traces": [{"count_count": "5"}]}

    assert await _source(fake).count("42", SINCE, UNTIL, {"t-1"}) == 5
    [query] = fake.queries
    assert {"column": "userId", "operator": "=", "value": "42", "type": "string"} in query[
        "filters"
    ]
    assert "timeDimension" not in query


def _trace(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "t-1",
        "name": "corpus.fixed",
        "timestamp": "2026-09-02T10:00:00.000Z",
        "environment": "production",
        "tags": [APP_TAG, "feature:corpus.fixed", "tool:issues:search", "lang:en"],
        "userId": "42",
        "input": {"question": "when is the coffee maker fixed?"},
        "output": {"answer": "Friday, says Bea in #ops"},
        "metadata": {
            "prompt_tokens": 900,
            "completion_tokens": 60,
            "evidence": [{"window_id": 3, "channel": "9"}],
            "decisions": [{"name": "route"}],
        },
        "totalCost": 0.0012,
    }
    row.update(overrides)
    return row


async def test_questions_keep_the_askers_words_and_metadata_only() -> None:
    fake = FakeMetrics()
    fake.traces = [_trace(input={"question": "x" * (MAX_QUESTION_CHARS + 50)})]

    page = await _source(fake).questions("42", SINCE, UNTIL, 2)

    [question] = page.questions
    assert question.question == "x" * MAX_QUESTION_CHARS
    assert (question.feature, question.tools) == ("corpus.fixed", ("issues:search",))
    assert (question.input_tokens, question.output_tokens, question.cost) == (900, 60, 0.0012)
    assert question.timestamp == datetime(2026, 9, 2, 10, tzinfo=UTC)
    assert (page.page, page.total_pages) == (2, 3)
    params = fake.requests[0].url.params
    assert params["userId"] == "42"
    assert params["environment"] == "production"
    assert params["tags"] == APP_TAG
    assert params["limit"] == "50"
    assert params["fromTimestamp"] == "2026-09-01T00:00:00Z"
    # The answer, the evidence and the decision trail are dropped here.
    assert "Friday" not in repr(page)
    assert "window_id" not in repr(page)


@pytest.mark.parametrize(
    "foreign",
    [
        {"environment": "staging"},
        {"tags": ["app:other", "feature:corpus.fixed"]},
        {"tags": []},
        {"name": "some-other-trace"},
        {"userId": "43"},
        {"timestamp": "not a time"},
    ],
    ids=["environment", "other-app", "untagged", "foreign-name", "other-asker", "no-time"],
)
async def test_a_row_the_server_should_have_filtered_is_dropped(foreign: dict[str, Any]) -> None:
    fake = FakeMetrics()
    fake.traces = [_trace(**foreign)]

    page = await _source(fake).questions("42", SINCE, UNTIL, 1)

    assert page.questions == ()
