"""Feature requests: reading what people suggested, and triaging it.

`GET /api/feature-requests` lists suggestions to operators and admins, newest
first, optionally one status at a time. Each carries the person's display
name as it is now and how many other people suggested the same text, and
never their platform id: the team reads a suggestion, it does not contact its
author from here.

`PATCH /api/feature-requests/{id}` is admin only (see `ROUTE_ACCESS`). It
sets the status, an admin note and a duplicate-of link, and every field that
changed is written to the change record with who changed it. Status and the
duplicate link are recorded with their values before and after; the note is
free text, so the record says only that it was set or cleared.

A suggestion is the person's own words, given to the team on purpose with a
disclosure. It is not corpus content: no message, document or ask body is
reachable from here. The author hears about a status change only if they
asked to, and from the bot process (`app.feature_request_news`); this
process cannot message anybody.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chatmemory.admin.audit import applied
from chatmemory.admin.auth import Actor
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.handlers.support import (
    Ok,
    Refused,
    acting_operator,
    body_of,
    moment,
    path_int,
)
from chatmemory.app.clock import utc_now
from chatmemory.ports.feature_requests import (
    MAX_NOTE_CHARS,
    RequestStatus,
    TriageChange,
    TriageEntry,
    TriageRefusal,
)

log = structlog.get_logger()

PAGE_SIZE = 50
FIELDS = frozenset({"status", "admin_note", "duplicate_of"})
"""Everything a PATCH may name. The text is the person's and is not editable."""

STATUS_VALUES = ", ".join(s.value for s in RequestStatus)


def routes(services: AdminServices) -> list[Route]:
    async def list_requests(request: Request) -> JSONResponse:
        status = _status_filter(request.query_params.get("status"))
        page = _page(request.query_params.get("page"))
        found = await services.feature_requests.triage_page(
            status, offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE
        )
        return JSONResponse(
            {
                "items": [_view(e) for e in found.entries],
                "total": found.total,
                "page": page,
                "page_size": PAGE_SIZE,
            }
        )

    async def triage(request: Request) -> JSONResponse:
        operator = acting_operator()
        request_id = path_int(request, "id")
        change = _change(await body_of(request), request_id)
        result = await services.feature_requests.triage(
            request_id, change, actor=operator.name, now=utc_now()
        )
        if result.refusal is TriageRefusal.UNKNOWN_DUPLICATE:
            raise Refused("duplicate_of names a suggestion that does not exist")
        if result.before is None or result.after is None:
            raise Refused("no such suggestion", status=404)
        await _record(services, operator, result.before, result.after)
        log.info("admin.feature_request_triaged", request_id=request_id, operator=operator.name)
        return Ok(changed=f"feature_request.{request_id}").response(
            request=_view(result.after)
        )

    return [
        Route("/api/feature-requests", list_requests, methods=["GET"], name="feature_requests"),
        Route(
            "/api/feature-requests/{id}",
            triage,
            methods=["PATCH"],
            name="triage_feature_request",
        ),
    ]


def _view(entry: TriageEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "text": entry.text,
        "language": entry.language,
        "status": entry.status.value,
        "admin_note": entry.admin_note,
        "duplicate_of": entry.duplicate_of,
        "source_kind": entry.source_kind.value,
        "person": entry.person_name,
        "same_text_elsewhere": entry.same_text_elsewhere,
        "created_at": moment(entry.created_at),
        "updated_at": moment(entry.updated_at),
        "updated_by": entry.updated_by,
    }


def _status_filter(raw: str | None) -> RequestStatus | None:
    if raw is None or not raw.strip():
        return None
    return _status(raw.strip())


def _status(raw: object) -> RequestStatus:
    if isinstance(raw, str) and raw in RequestStatus._value2member_map_:
        return RequestStatus(raw)
    raise Refused(f"status must be one of: {STATUS_VALUES}")


def _page(raw: str | None) -> int:
    if raw is None or not raw.strip().isdigit():
        return 1
    return max(int(raw), 1)


def _change(body: Mapping[str, Any], request_id: int) -> TriageChange:
    unknown = set(body) - FIELDS
    if unknown:
        raise Refused(f"only {', '.join(sorted(FIELDS))} can be changed")
    if not body:
        raise Refused("nothing to change")
    return TriageChange(
        status=_status(body["status"]) if "status" in body else None,
        set_note="admin_note" in body,
        admin_note=_note(body.get("admin_note")),
        set_duplicate="duplicate_of" in body,
        duplicate_of=_duplicate(body.get("duplicate_of"), request_id),
    )


def _note(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise Refused("admin_note must be a string or null")
    note = raw.strip()
    if len(note) > MAX_NOTE_CHARS:
        raise Refused(f"admin_note is limited to {MAX_NOTE_CHARS} characters")
    return note or None


def _duplicate(raw: object, request_id: int) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        raise Refused("duplicate_of must be a suggestion number or null")
    text = str(raw).strip()
    if not text.isdigit():
        raise Refused("duplicate_of must be a suggestion number or null")
    target = int(text)
    if target == request_id:
        raise Refused("a suggestion cannot be a duplicate of itself")
    return target


async def _record(
    services: AdminServices, operator: Actor, before: TriageEntry, after: TriageEntry
) -> None:
    """One entry per field that changed. The note's text is never recorded."""
    setting = f"feature_request.{after.id}"
    if before.status is not after.status:
        await services.changes.record(
            applied(operator, f"{setting}.status", before.status.value, after.status.value)
        )
    if before.duplicate_of != after.duplicate_of:
        await services.changes.record(
            applied(
                operator,
                f"{setting}.duplicate_of",
                _number(before.duplicate_of),
                _number(after.duplicate_of),
            )
        )
    if before.admin_note != after.admin_note:
        await services.changes.record(
            applied(
                operator,
                f"{setting}.admin_note",
                _presence(before.admin_note),
                _presence(after.admin_note),
            )
        )


def _number(value: int | None) -> str | None:
    return None if value is None else f"#{value}"


def _presence(note: str | None) -> str:
    return "set" if note else "empty"
