"""GET /api/audit -- the change record, read back.

Named `changes` rather than `audit` because it is the *read* side of
`chatmemory.admin.audit`, and one module that both writes and serves the
record would eventually grow a route that edits it.

There is no such route here, and there is none anywhere: the port has `record`
and `recent`, and migration 0012 puts a trigger behind the table that refuses
UPDATE and DELETE outright. "The console provides no means of altering or
removing it" is therefore a shape rather than a rule somebody remembers.

Refused attempts are returned alongside applied ones on purpose. After an
incident the interesting question is usually "what did it decline to do", and
a log of successes cannot answer it.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chatmemory.admin.audit import ChangeRecord
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.handlers.support import moment

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
"""A bound, so one request cannot ask for the whole history of a long-lived
deployment and time out the screen that was meant to show it."""


def routes(services: AdminServices) -> list[Route]:
    async def audit(request: Request) -> JSONResponse:
        limit = _limit(request.query_params.get("limit"))
        entries = await services.changes.recent(limit)
        return JSONResponse([_view(e) for e in entries])

    return [Route("/api/audit", audit, methods=["GET"], name="audit")]


def _limit(raw: str | None) -> int:
    if raw is None or not raw.strip().isdigit():
        return DEFAULT_LIMIT
    return min(int(raw), MAX_LIMIT)


def _view(entry: ChangeRecord) -> dict[str, Any]:
    return {
        "sequence": entry.sequence,
        "operator": entry.operator,
        "setting": entry.setting,
        "before": entry.before,
        "after": entry.after,
        # applied, refused or escalation. The console renders an escalation
        # differently: "what got more permissive, and who did it" has to be a
        # filter rather than a reading exercise.
        "kind": str(entry.kind),
        "reason": entry.reason,
        "at": moment(entry.recorded_at),
    }
