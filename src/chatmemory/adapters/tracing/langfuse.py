"""Langfuse, over its ingestion API, with the answer path never waiting on it.

Written against the HTTP API rather than the `langfuse` SDK deliberately. The
SDK owns a background thread, a queue and a flush policy, and the one property
this adapter must have -- that a slow or dead destination costs an operator a
trace and never costs a requester an answer -- would then belong to somebody
else's scheduler. One POST with a timeout is a property you can read off the
code.

What is sent is the question as asked, the answer as sent, the decision trail
and a reference to each piece of evidence -- its window, channel, source
system and score, never its text. Evidence quotes other people's messages,
private channels and DMs included, and anyone with a Langfuse login could read
it there; the reference is enough to study a retrieval against the corpus,
where viewer scoping still applies. `trace_export_message` still records which
messages a trace drew on, so deleting one withdraws the traces built from it.

A trace is named by its feature and tagged with this application's tag, so
that every read and delete against a shared Langfuse project can be scoped to
this application and environment.

Each model call rides in the same batch as a `generation-create` (stage,
model, input and output tokens), from which Langfuse prices the run, and each
federated tool call as a `span-create` (name, outcome, latency). Tool
arguments are never sent: they can hold a wallet address or a query.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from chatmemory.app.reasoning.budgets import ModelUsage, ToolUsage
from chatmemory.app.reasoning.contract import AnswerPath, RunTrace
from chatmemory.app.reasoning.features import FEATURES
from chatmemory.app.reasoning.loop import FEDERATION_CALL
from chatmemory.ports.tracing import TraceIndex

log = structlog.get_logger()

DEFAULT_TIMEOUT = 5.0

APP_TAG = "app:cyberfriend"
"""Carried by every trace this application exports; reads and deletes filter on it."""

#: The names traces were given before features and the app tag existed. Only
#: these are recognised without `APP_TAG`: they predate it.
LEGACY_TRACE_NAMES = frozenset(str(path) for path in AnswerPath)
#: The names this application gives its traces: the feature ids, and the
#: legacy path names. A feature id is a generic word (`time`, `portfolio`), so
#: a row with one is ours only if it also carries `APP_TAG`.
TRACE_NAMES = FEATURES | LEGACY_TRACE_NAMES
#: Langfuse's largest page for the traces list.
SEARCH_PAGE_SIZE = 100


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LangfuseTracer:
    """Implements `RunTracer` against a Langfuse deployment."""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        index: TraceIndex | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        environment: str = "production",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = host.rstrip("/") + "/api/public/ingestion"
        self._auth = (public_key, secret_key)
        self._index = index
        self._client = client
        self._timeout = timeout
        self._environment = environment
        self._transport = transport

    async def trace(self, run: RunTrace) -> None:
        trace_id = str(uuid.uuid4())
        try:
            await self._send(self._batch(trace_id, run))
        except Exception as exc:  # noqa: BLE001 - tracing never fails a reply
            log.warning("tracing.export_failed", error=str(exc))
            return
        if self._index is not None:
            # Only after the destination accepted it. Recording the mapping
            # for a trace that was never stored would leave a deletion chasing
            # something that does not exist.
            await self._index.record_export(
                trace_id, _message_ids(run), run.question.asker.person
            )

    # --- building ------------------------------------------------------

    def _batch(self, trace_id: str, run: RunTrace) -> dict[str, Any]:
        now = _now()
        body: dict[str, Any] = {
            "id": trace_id,
            "name": _name(run),
            "timestamp": now,
            "environment": self._environment,
            "tags": _tags(run),
            # The asker, so a run can be followed without joining anything.
            # Their platform id, which is what the corpus keys on; no display
            # name, because that is a second copy of somebody's identity in a
            # store that does not need one to be useful.
            "userId": str(run.question.asker.person.platform_user_id),
            "input": {"question": run.question.text},
            "output": {
                "answer": run.answer.text,
                "abstained": run.answer.abstained,
                "partial": run.answer.partial,
            },
            "metadata": {
                "status": str(run.record.status),
                "cause": str(run.record.cause),
                "path": str(run.record.path),
                "model_calls": run.record.spend.model_calls,
                "tool_calls": run.record.spend.tool_calls,
                "prompt_tokens": run.record.spend.prompt_tokens,
                "completion_tokens": run.record.spend.completion_tokens,
                "elapsed_seconds": round(run.record.spend.elapsed_seconds, 3),
                "queries": list(run.record.queries),
                "sub_questions": list(run.record.sub_questions),
                "decisions": [
                    {
                        "name": d.name,
                        "outcome": d.outcome,
                        "made_by": str(d.made_by),
                        "detail": d.detail,
                    }
                    for d in run.record.decisions
                ],
                "blocked": [str(b.action) for b in run.record.blocked_actions],
                "evidence": [_evidence(e) for e in run.evidence],
                "citations": [_citation(c) for c in run.answer.citations],
            },
        }
        events = [_event("trace-create", now, body)]
        events.extend(
            _event("generation-create", now, _generation(trace_id, usage))
            for usage in run.record.spend.models
        )
        events.extend(
            _event("span-create", now, _span(trace_id, usage))
            for usage in run.record.spend.tools
        )
        # One batch, one POST: the answer path pays no extra round-trip for
        # the generations and spans.
        return {"batch": events}

    # --- transport -----------------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        # Bounded twice, and the outer one is the bound that matters. httpx's
        # timeout covers its own network phases; `wait_for` covers the call,
        # whatever the transport does with it. The export sits between writing
        # the answer and returning it, so this is time somebody spends waiting
        # for something that is not for them -- and a ceiling that depends on
        # a library honouring a keyword argument is not a ceiling.
        await asyncio.wait_for(self._post(payload), timeout=self._timeout)

    async def _post(self, payload: dict[str, Any]) -> None:
        if self._client is not None:
            response = await self._client.post(
                self._url, json=payload, auth=self._auth, timeout=self._timeout
            )
            response.raise_for_status()
            return
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await client.post(self._url, json=payload, auth=self._auth)
            response.raise_for_status()


class LangfuseTraceDeleter:
    """Implements `TraceDeleter`. Returns rather than raises, by contract."""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = host.rstrip("/") + "/api/public/traces"
        self._auth = (public_key, secret_key)
        self._client = client
        self._timeout = timeout
        self._transport = transport

    async def delete_traces(self, trace_ids: Sequence[str]) -> bool:
        if not trace_ids:
            return True
        payload = {"traceIds": list(trace_ids)}
        try:
            if self._client is not None:
                response = await self._client.request(
                    "DELETE", self._url, json=payload,
                    auth=self._auth, timeout=self._timeout,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport
                ) as client:
                    response = await client.request(
                        "DELETE", self._url, json=payload, auth=self._auth
                    )
            response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 - a deletion retries, never raises
            log.warning("tracing.delete_failed", count=len(trace_ids), error=str(exc))
            return False


class LangfuseTraceFinder:
    """Implements `TraceFinder` and `ExpiredTraceFinder` over `GET /api/public/traces`.

    Filtered twice: the query asks for our environment, and every row is
    checked for our environment and one of our trace names before its id is
    returned, so a server that ignores a filter still cannot widen a deletion
    to another application's traces. A row named by a feature must also carry
    `APP_TAG`: another application in a shared project may well name a trace
    `time`. Only the legacy path names, which predate the tag, pass without it.
    """

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        environment: str = "production",
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = host.rstrip("/") + "/api/public/traces"
        self._auth = (public_key, secret_key)
        self._environment = environment
        self._timeout = timeout
        self._transport = transport

    async def find_traces_by_user(self, platform_user_id: int) -> Sequence[str] | None:
        query = {"userId": str(platform_user_id)}
        return await self._search([query], self._is_ours)

    async def find_traces_before(self, cutoff: datetime) -> Sequence[str] | None:
        """Our traces older than `cutoff`: tagged ones, then untagged legacy names.

        Every row is also checked against the cutoff, for the same reason
        every row is checked against the environment.
        """
        bound = {"toTimestamp": cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")}
        queries = [{**bound, "tags": APP_TAG}] + [
            {**bound, "name": name} for name in sorted(LEGACY_TRACE_NAMES)
        ]
        return await self._search(
            queries, lambda row: self._is_ours(row) and _before(row, cutoff)
        )

    async def _search(
        self,
        queries: Sequence[dict[str, str]],
        keep: Callable[[dict[str, Any]], bool],
    ) -> list[str] | None:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                found: dict[str, None] = {}
                for query in queries:
                    rows = await self._all_pages(client, query)
                    found.update((str(r["id"]), None) for r in rows if r.get("id") and keep(r))
                return list(found)
        except Exception as exc:  # noqa: BLE001 - the search retries, never raises
            log.warning("tracing.search_failed", error=str(exc))
            return None

    async def _all_pages(
        self, client: httpx.AsyncClient, query: dict[str, str]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await client.get(
                self._url, auth=self._auth, params=self._params(query, page)
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data") or []
            rows.extend(data)
            total_pages = int((body.get("meta") or {}).get("totalPages") or 0)
            if not data or page >= total_pages:
                return rows
            page += 1

    def _params(self, query: dict[str, str], page: int) -> dict[str, str | int]:
        return {
            **query,
            "environment": self._environment,
            "fields": "core",
            "limit": SEARCH_PAGE_SIZE,
            "page": page,
        }

    def _is_ours(self, row: dict[str, Any]) -> bool:
        if row.get("environment") != self._environment:
            return False
        name = row.get("name")
        if name in LEGACY_TRACE_NAMES:
            return True
        return name in FEATURES and APP_TAG in (row.get("tags") or ())


def _before(row: dict[str, Any], cutoff: datetime) -> bool:
    """Whether the row's timestamp is before `cutoff`; a row without one is not."""
    try:
        stamp = datetime.fromisoformat(str(row["timestamp"]))
    except (KeyError, ValueError):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp < cutoff


SUPPORTED_MAJOR = 3
"""The Langfuse major version this adapter is written against.

v4 answers `trace-create` ingestion with a 400 and drops the v1 reads, so a
v4 deployment would silently stop recording. The Coolify service is pinned
(`docs/operations.md`); this is the check that says so when the pin slips.
"""


async def langfuse_major_version(
    host: str,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int | None:
    """The major version `/api/public/health` reports, or None if it says none."""
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        response = await client.get(host.rstrip("/") + "/api/public/health")
        response.raise_for_status()
        version = str(response.json().get("version") or "")
    major = version.split(".", 1)[0]
    return int(major) if major.isdigit() else None


async def warn_unless_supported(
    host: str,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int | None:
    """Log a warning when Langfuse is not on the supported major version.

    Best effort, and never raises: a trace store that cannot be reached must
    not stop a process from starting. Returns the major version seen.
    """
    try:
        major = await langfuse_major_version(host, timeout, transport)
    except Exception as exc:  # noqa: BLE001 - a startup check, never a startup failure
        log.warning("tracing.langfuse_health_unreadable", error=str(exc))
        return None
    if major != SUPPORTED_MAJOR:
        log.warning(
            "tracing.langfuse_unsupported_version",
            major=major,
            supported=SUPPORTED_MAJOR,
        )
    return major


def _event(kind: str, now: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "type": kind, "timestamp": now, "body": body}


def _generation(trace_id: str, usage: ModelUsage) -> dict[str, Any]:
    """One model call. Langfuse prices it from `model` and `usageDetails`."""
    body: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "traceId": trace_id,
        "name": usage.stage or "model",
        "startTime": usage.started_at.isoformat(),
        "endTime": usage.ended_at.isoformat(),
        "usageDetails": {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
        },
    }
    if usage.model:
        # Omitted rather than sent empty: an unnamed model matches no price.
        body["model"] = usage.model
    return body


def _span(trace_id: str, usage: ToolUsage) -> dict[str, Any]:
    """One federated tool call: name, outcome and latency, never its arguments."""
    return {
        "id": str(uuid.uuid4()),
        "traceId": trace_id,
        "name": usage.name,
        "startTime": usage.started_at.isoformat(),
        "endTime": usage.ended_at.isoformat(),
        "level": "ERROR" if usage.failed else "DEFAULT",
        "statusMessage": usage.outcome,
        "metadata": {"outcome": usage.outcome},
    }


def _name(run: RunTrace) -> str:
    """The feature, or the path for a record no answer service named."""
    return run.record.feature or str(run.record.path)


def _tags(run: RunTrace) -> list[str]:
    """What the metrics API groups by and the traces list filters by."""
    tags = [
        APP_TAG,
        f"feature:{_name(run)}",
        f"path:{run.record.path}",
        f"lang:{run.language or 'unknown'}",
    ]
    tags.extend(f"tool:{name}" for name in _tools(run))
    return tags


def _tools(run: RunTrace) -> list[str]:
    """The federated tools the run asked to call, by qualified name.

    Names only: the arguments can hold a wallet address or a query, and are
    never exported.
    """
    called = {
        d.detail
        for d in run.record.decisions
        if d.name == FEDERATION_CALL and d.outcome == "tool_requested" and d.detail
    }
    return sorted(called)


def _evidence(item: Any) -> dict[str, Any]:
    """A reference to the evidence, never its text or any excerpt of it."""
    return {
        "window_id": item.window_id,
        "channel": str(item.channel),
        "source_system": item.source_system,
        "score": round(item.score, 4),
    }


def _citation(item: Any) -> dict[str, Any]:
    """Which message a citation points at, never what it quotes."""
    return {
        "channel": str(item.channel),
        "message_id": item.message_id,
        "source_system": item.source_system,
    }


def _message_ids(run: RunTrace) -> list[int]:
    """Every corpus message the trace draws on: its evidence and its citations.

    Citations count because an answer given from records -- an obligation, a
    decision -- quotes its source message without holding evidence for it.
    External evidence carries no message ids, so nothing is recorded for it --
    which is right: there is no tombstone coming for a Wikipedia article.
    """
    ids: set[int] = set()
    for item in run.evidence:
        if item.from_corpus:
            ids.update(item.message_ids)
    ids.update(c.message_id for c in run.answer.citations if c.is_corpus)
    return sorted(ids)
