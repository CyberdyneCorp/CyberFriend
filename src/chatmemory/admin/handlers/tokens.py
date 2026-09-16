"""MCP credentials: who may query the corpus through the agent's endpoint.

These are not console credentials. A console token names an operator and lets
them change configuration; an MCP token names a *person* and lets them read
exactly what that person may read in Discord.

**The console cannot mint one.** That is the load-bearing property of this
module, and it is why there is no POST here. Minting is not an ordinary
configuration change: the request would name a Discord account, and the
credential it returned would read, through `mcp/auth.py`, every channel that
account can see. An operator holding only a console credential would then hold
a route to message content -- exactly the thing `handlers/services.py` says the
console has no route to, reached by asking the console for the key rather than
by asking it for the content. Recording the act as an escalation would document
it; it would not bound it.

So issuing stays where it takes the agent's own environment and a shell on the
host: `python -m chatmemory.mcp.issue_token`. The console is handed a
`TokenDirectory` rather than a `TokenStore`, so a re-added mint route fails to
type-check instead of shipping.

What is left is the half that cannot widen anything:

*   **Reviewing.** The listing carries a label, a person and timestamps -- no
    credential and no hash. The stored hash is what an attacker with a
    database dump already has; handing it out over HTTP adds nothing an
    operator needs and one more place it can leak from.
*   **Revoking.** Withdrawing access is the operation you want reachable from
    a browser at 3am, and it can only take access away.

Revocation is per person, because that is the granularity the store works at
and the granularity that makes revoking somebody realistic: it never becomes
an outage for everybody else.
"""

from __future__ import annotations

from typing import Any

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chatmemory.admin.audit import applied
from chatmemory.admin.handlers.optouts import PLATFORM
from chatmemory.admin.handlers.services import AdminServices
from chatmemory.admin.handlers.support import (
    Ok,
    Refused,
    acting_operator,
    moment,
)
from chatmemory.app.tokens import TokenRecord
from chatmemory.domain.identity import PersonRef

log = structlog.get_logger()


def routes(services: AdminServices) -> list[Route]:
    async def list_tokens(_: Request) -> JSONResponse:
        records = await services.mcp_tokens.active_tokens()
        return JSONResponse([_view(r) for r in records])

    async def revoke_token(request: Request) -> JSONResponse:
        operator = acting_operator()
        person = _parse_id(str(request.path_params["id"]))
        before = await _live_credentials(services, person)
        revoked = await services.mcp_tokens.revoke(person)
        if not revoked:
            raise Refused(f"{person} holds no live credential", status=404)

        await services.changes.record(
            applied(
                operator,
                setting=f"mcp_token:{person}",
                before=f"{before} live credential(s)",
                after=f"{before - revoked} live credential(s)",
            )
        )
        log.info(
            "admin.mcp_token_revoked",
            person=str(person),
            revoked=revoked,
            operator=operator.name,
        )
        return Ok(
            changed=f"mcp_token:{person}", detail=f"revoked {revoked} credential(s)"
        ).response()

    return [
        Route("/api/tokens", list_tokens, methods=["GET"], name="tokens"),
        Route("/api/tokens/{id}", revoke_token, methods=["DELETE"], name="revoke_token"),
    ]


def _view(record: TokenRecord) -> dict[str, Any]:
    return {
        # The id a caller deletes by. Revocation is per person, so the person
        # *is* the identifier; a per-credential id would promise a granularity
        # the store does not have.
        "id": str(record.person),
        "person": str(record.person),
        "label": record.label,
        "issued_at": moment(record.issued_at),
        "revoked_at": moment(record.revoked_at),
    }


def _person(platform: str, user_id: int) -> PersonRef:
    if not PLATFORM.match(platform):
        raise Refused(f"platform must match {PLATFORM.pattern}")
    return PersonRef(platform, user_id)


def _parse_id(raw: str) -> PersonRef:
    platform, _, user_id = raw.partition(":")
    if not user_id.lstrip("-").isdigit():
        raise Refused("a token id is written platform:account_id")
    return _person(platform, int(user_id))


async def _live_credentials(services: AdminServices, person: PersonRef) -> int:
    """How many working credentials this person holds right now.

    Counted so the change record carries a before and an after rather than
    only the fact that something happened -- the same reason
    `OperatorTokens.live_credentials` exists for console credentials.
    """
    records = await services.mcp_tokens.active_tokens()
    return sum(1 for r in records if r.person == person)
