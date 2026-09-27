"""Usage and cost of traced question runs, and the one route that returns text.

`GET /api/usage/summary` (operator) returns counts, tokens, estimated cost,
tool calls and voice seconds, grouped by person, feature, model or tool, for
a window of at most 90 days. Counts only.

`GET /api/usage/people/{id}/questions` (`admin_oidc`: an admin signed in
through CyberdyneAuth, never a `cfa_` token) returns the questions one person
asked, and nothing else of the trace: no answer, no evidence. Only questions
traced after the person was told they are recorded come back as text; earlier
ones are a count. Every call is recorded in the change record against the
viewer's `sub`, with the viewed person looked up here rather than taken from
the request, before the trace store is read. The response is `no-store`.

When the trace store cannot be read, both answer 503 "usage unavailable",
never stale or partial figures.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chatmemory.admin.audit import applied
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.handlers.support import Refused, acting_operator
from chatmemory.app.usage import (
    BadPage,
    BadWindow,
    Grouping,
    QuestionsView,
    UsageLine,
    UsageSummary,
    page_of,
    platform_id,
    window_of,
)
from chatmemory.ports.usage import UsageUnavailable, UsageWindow, ViewedPerson

log = structlog.get_logger()

UNAVAILABLE = "usage unavailable"
VIEWED_SETTING = "usage.questions_viewed"
NO_STORE = {"Cache-Control": "no-store"}


def routes(services: AdminServices) -> list[Route]:
    usage = services.usage

    async def summary(request: Request) -> JSONResponse:
        window = _window(request)
        grouping = _grouping(request.query_params.get("group"))
        try:
            found = await usage.summary(window, grouping)
        except UsageUnavailable:
            return _unavailable()
        return JSONResponse(_summary(found))

    async def questions(request: Request) -> JSONResponse:
        user_id = platform_id(str(request.path_params["id"]))
        if user_id is None:
            raise Refused("id must be a numeric platform account id")
        window = _window(request)
        page = _page(request)
        person = await usage.person(user_id)
        viewer = acting_operator()
        # Recorded before the trace store is read: an attempt to view is on
        # the record even when Langfuse is down.
        await services.changes.record(
            applied(viewer, VIEWED_SETTING, None, _viewed(user_id, person, window, page))
        )
        log.info("admin.usage_questions_viewed", viewer=viewer.name, person=user_id)
        try:
            view = await usage.questions(user_id, person, window, page)
        except UsageUnavailable:
            return _unavailable(NO_STORE)
        return JSONResponse(_questions(view, window), headers=NO_STORE)

    return [
        Route("/api/usage/summary", summary, methods=["GET"], name="usage_summary"),
        Route(
            "/api/usage/people/{id}/questions",
            questions,
            methods=["GET"],
            name="usage_questions",
        ),
    ]


def _window(request: Request) -> UsageWindow:
    try:
        return window_of(
            request.query_params.get("from"),
            request.query_params.get("to"),
            datetime.now(UTC).date(),
        )
    except BadWindow as exc:
        raise Refused(str(exc)) from exc


def _page(request: Request) -> int:
    try:
        return page_of(request.query_params.get("page"))
    except BadPage as exc:
        raise Refused(str(exc)) from exc


def _grouping(raw: str | None) -> Grouping:
    try:
        return Grouping(raw or Grouping.PERSON)
    except ValueError as exc:
        raise Refused("group must be person, feature, model or tool") from exc


def _unavailable(headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": UNAVAILABLE}, status_code=503, headers=headers)


def _bounds(window: UsageWindow) -> dict[str, str]:
    return {
        "from": window.start.date().isoformat(),
        "to": (window.end - timedelta(days=1)).date().isoformat(),
    }


def _viewed(
    user_id: str, person: ViewedPerson | None, window: UsageWindow, page: int
) -> str:
    who = (
        f"person {person.person_id} ({person.display_name or 'no name'})"
        if person
        else "no person on record"
    )
    bounds = _bounds(window)
    return f"{who}, platform id {user_id}, {bounds['from']}..{bounds['to']}, page {page}"


def _summary(found: UsageSummary) -> dict[str, Any]:
    totals = found.totals
    return {
        **_bounds(found.window),
        "group": str(found.grouping),
        "label": found.label,
        "rows": [_line(line) for line in found.lines],
        "totals": {
            "questions": totals.questions,
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "cost": round(totals.cost, 6),
            "tool_calls": totals.tool_calls,
            "voice_seconds": totals.voice_seconds,
        },
    }


def _line(line: UsageLine) -> dict[str, Any]:
    return {
        "key": line.key,
        "name": line.name,
        "questions": line.questions,
        "input_tokens": line.input_tokens,
        "output_tokens": line.output_tokens,
        "cost": None if line.cost is None else round(line.cost, 6),
        "tool_calls": line.tool_calls,
        "tools": sorted(line.tools),
        "voice_seconds": line.voice_seconds,
    }


def _questions(view: QuestionsView, window: UsageWindow) -> dict[str, Any]:
    person = view.person
    return {
        **_bounds(window),
        "person": {
            "platform_user_id": view.user_id,
            "person_id": person.person_id if person else None,
            "name": (person.display_name or None) if person else None,
        },
        "page": view.page,
        "total_pages": view.total_pages,
        "hidden_before_notice": view.hidden_before_notice,
        # The asker's own words and metadata, nothing else of the trace.
        "questions": [
            {
                "timestamp": q.timestamp.isoformat(),
                "feature": q.feature,
                "tools": list(q.tools),
                "question": q.question,
                "input_tokens": q.input_tokens,
                "output_tokens": q.output_tokens,
                "cost": q.cost,
            }
            for q in view.questions
        ],
    }
