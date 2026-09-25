"""Langfuse, over its ingestion API, with the answer path never waiting on it.

Written against the HTTP API rather than the `langfuse` SDK deliberately. The
SDK owns a background thread, a queue and a flush policy, and the one property
this adapter must have -- that a slow or dead destination costs an operator a
trace and never costs a requester an answer -- would then belong to somebody
else's scheduler. One POST with a timeout is a property you can read off the
code.

What is sent is the whole run: the question as asked, the answer as sent, the
decision trail, and every piece of evidence with its text. That is what makes
it useful for improving the assistant, and it is also why the destination
holds private-channel content with no viewer scoping. That trade is stated in
the proposal and in the operator documentation; it is not this file's to make.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from chatmemory.app.reasoning.contract import AnswerPath, RunTrace
from chatmemory.ports.tracing import TraceIndex

log = structlog.get_logger()

DEFAULT_TIMEOUT = 5.0
#: Enough to study a retrieval, far short of a database dump in a JSON field.
MAX_EVIDENCE_CHARS = 4000

#: The names this application gives its traces (`_batch` names a trace after
#: its path). A search of a shared Langfuse project deletes nothing else.
TRACE_NAMES = frozenset(str(path) for path in AnswerPath)
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
            "name": str(run.record.path),
            "timestamp": now,
            "environment": self._environment,
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
                "citations": [c.excerpt for c in run.answer.citations],
            },
        }
        return {
            "batch": [
                {
                    "id": str(uuid.uuid4()),
                    "type": "trace-create",
                    "timestamp": now,
                    "body": body,
                }
            ]
        }

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
    """Implements `TraceFinder` over `GET /api/public/traces`.

    Filtered twice: the query asks for our environment, and every row is
    checked for our environment and one of our trace names before its id is
    returned, so a server that ignores a filter still cannot widen a deletion
    to another application's traces.
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
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                return await self._all_pages(client, platform_user_id)
        except Exception as exc:  # noqa: BLE001 - the search retries, never raises
            log.warning("tracing.search_failed", error=str(exc))
            return None

    async def _all_pages(
        self, client: httpx.AsyncClient, platform_user_id: int
    ) -> list[str]:
        found: list[str] = []
        page = 1
        while True:
            response = await client.get(
                self._url, auth=self._auth, params=self._query(platform_user_id, page)
            )
            response.raise_for_status()
            body = response.json()
            rows = body.get("data") or []
            found.extend(self._ours(rows))
            total_pages = int((body.get("meta") or {}).get("totalPages") or 0)
            if not rows or page >= total_pages:
                return found
            page += 1

    def _query(self, platform_user_id: int, page: int) -> dict[str, str | int]:
        return {
            "userId": str(platform_user_id),
            "environment": self._environment,
            "fields": "core",
            "limit": SEARCH_PAGE_SIZE,
            "page": page,
        }

    def _ours(self, rows: Sequence[dict[str, Any]]) -> list[str]:
        return [
            str(row["id"])
            for row in rows
            if row.get("environment") == self._environment
            and row.get("name") in TRACE_NAMES
            and row.get("id")
        ]


def _evidence(item: Any) -> dict[str, Any]:
    return {
        "window_id": item.window_id,
        "channel": str(item.channel),
        "source_system": item.source_system,
        "score": round(item.score, 4),
        "relevance": str(item.relevance_source),
        "author": item.author_display,
        "url": item.url,
        "text": item.text[:MAX_EVIDENCE_CHARS],
    }


def _message_ids(run: RunTrace) -> list[int]:
    """Every corpus message the trace quotes.

    External evidence carries no message ids, so nothing is recorded for it --
    which is right: there is no tombstone coming for a Wikipedia article.
    """
    ids: set[int] = set()
    for item in run.evidence:
        if item.from_corpus:
            ids.update(item.message_ids)
    return sorted(ids)
