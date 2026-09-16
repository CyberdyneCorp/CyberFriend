"""Per-person opt-out: who has withdrawn, and withdrawing somebody.

Recording an opt-out is not a flag, it is a purge: the messages, the windows
built over them, the asks, the reactions, the mentions and the documents the
person uploaded. The response says how much of each went, in counts, because
an operator acting on somebody's request has to be able to tell them it
happened -- and because a number is the only form of that answer which does
not quote what was removed.

Opting somebody back in restores nothing. That asymmetry is deliberate and is
documented in docs/operations.md; the response repeats it, because an operator
who believes otherwise will tell somebody else the wrong thing.

The list deliberately carries who and since, and not the reason text stored
beside the exclusion. The console asks for a list of who has withdrawn, not
for a file on them.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chatmemory.admin.audit import applied
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.handlers.support import (
    Ok,
    Refused,
    account_id,
    acting_operator,
    body_of,
    moment,
    text_field,
)
from chatmemory.app.optout import OptOutReport
from chatmemory.domain.identity import PersonRef

log = structlog.get_logger()

PLATFORM = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")
"""Platforms are constrained because recording an opt-out *creates* the person
row it keys on. A typo'd platform would silently create an exclusion nobody can
find and leave the real account still being ingested."""


def routes(services: AdminServices) -> list[Route]:
    async def list_optouts(_: Request) -> JSONResponse:
        entries = await services.optout_directory.opted_out()
        return JSONResponse(
            [{"person": str(e), "since": moment(e.since)} for e in entries]
        )

    async def add_optout(request: Request) -> JSONResponse:
        operator = acting_operator()
        body = await body_of(request)
        person = _person(text_field(body, "platform"), account_id(body, "platform_user_id"))

        report = await services.optouts.opt_out(person, reason=f"console:{operator.name}")
        await services.changes.record(
            applied(
                operator,
                setting=f"opt_out:{person}",
                before="included in the corpus",
                after=f"excluded; {report.total} record(s) removed",
            )
        )
        log.info("admin.optout_recorded", person=str(person), operator=operator.name)
        return Ok(
            changed=f"opt_out:{person}",
            detail=(
                "the exclusion is enforced in the database, so a backfill cannot "
                "re-import what was just removed"
            ),
        ).response(removed=_removed(report))

    async def remove_optout(request: Request) -> JSONResponse:
        operator = acting_operator()
        person = _person(
            str(request.path_params["platform"]), _numeric(str(request.path_params["id"]))
        )
        # An ordinary change, not an escalation. The escalation filter answers
        # "what did somebody grant the agent"; a person's own decision to stop
        # being excluded is not that, and filing it there would dilute the one
        # filter that has to stay readable.
        await services.optouts.opt_in(person)
        await services.changes.record(
            applied(
                operator,
                setting=f"opt_out:{person}",
                before="excluded from the corpus",
                after="included again from now on",
            )
        )
        log.info("admin.optout_cleared", person=str(person), operator=operator.name)
        return Ok(
            changed=f"opt_out:{person}",
            detail=(
                "future messages are archived again. Nothing purged comes back; only "
                "what the platform still holds and a later backfill re-reads."
            ),
        ).response()

    return [
        Route("/api/optouts", list_optouts, methods=["GET"], name="optouts"),
        Route("/api/optouts", add_optout, methods=["POST"], name="add_optout"),
        Route(
            "/api/optouts/{platform}/{id}",
            remove_optout,
            methods=["DELETE"],
            name="remove_optout",
        ),
    ]


def _person(platform: str, user_id: int) -> PersonRef:
    if not PLATFORM.match(platform):
        raise Refused(f"platform must match {PLATFORM.pattern}")
    return PersonRef(platform, user_id)


def _numeric(raw: str) -> int:
    if not raw.lstrip("-").isdigit():
        raise Refused("the account id must be numeric")
    return int(raw)


def _removed(report: OptOutReport) -> dict[str, Any]:
    """What the purge took, as counts. Never as content."""
    return {
        "windows": report.corpus.windows,
        "messages": report.corpus.messages,
        "asks": report.corpus.asks,
        "reactions": report.corpus.reactions,
        "mentions": report.corpus.mentions,
        "documents": report.documents,
        "total": report.total,
    }
