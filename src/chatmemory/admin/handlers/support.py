"""What every console handler shares: refusals, bodies, and who is acting.

The shapes here exist so that the interesting decisions in each handler are
not buried in argument checking. Three of them carry weight:

*   `Refused` is raised, not returned. A handler that has decided to refuse
    must not be able to carry on and write something anyway, and an exception
    is the only shape that guarantees it.
*   `body_of` refuses anything that is not a JSON object. A request whose
    body is a list or a bare string reaching a field lookup would raise
    `TypeError` and surface as a 500, which reads as a bug in the console
    rather than as a malformed request.
*   `acting_operator` is a one-line wrapper over `current_actor`, and the
    only way a handler learns who is acting. It reads the context the
    middleware bound from the credential; it never reads the request. A
    handler cannot accidentally take an operator from a body field, because
    there is no function here that would give it one.

No function here echoes a submitted value back, for the same reason the audit
refuses to record one: the likeliest bad value on this surface is a credential
pasted into the wrong box, and a refusal that quoted it would put it straight
into an error body and from there into whatever the console logs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse

from chatmemory.admin.auth import Actor, current_actor

log = structlog.get_logger()

#: What a handler says when it will not do something. The wording is
#: deliberately about the console's own rules; it never quotes the value that
#: was submitted, and never says what a credential looked like.
ERROR_KEY = "error"


class Refused(Exception):
    """A request the console will not carry out, with the status to send.

    Carries no value from the request. The detail is written by the handler
    and describes the rule, so a caller pasting a credential into a settings
    field gets "credentials are read from the environment", not their own
    credential echoed back at them.
    """

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def refusal(request: Request, exc: Exception) -> JSONResponse:
    """Render a refusal. Registered on the app so every route shares it."""
    if isinstance(exc, Refused):
        return JSONResponse({ERROR_KEY: exc.detail}, status_code=exc.status)
    # Unreachable while handlers raise `Refused`; kept because an unexpected
    # exception must not render a stack trace into an operator's browser.
    log.exception("admin.handler_failed", path=request.url.path)
    return JSONResponse({ERROR_KEY: "the console could not complete that"}, status_code=500)


def acting_operator() -> Actor:
    """Who this request acts as, from the credential and from nothing else.

    An operator name for a `cfa_` token; `oidc:<sub>`, with the email to show
    beside it, for a CyberdyneAuth session.
    """
    return current_actor()


async def body_of(request: Request) -> Mapping[str, Any]:
    """The request's JSON object, or a refusal.

    Deliberately strict about the shape. A body that is not an object has no
    fields to read, and letting it through would turn a malformed request
    into an internal error.
    """
    try:
        parsed = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise Refused("the request body is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise Refused("the request body must be a JSON object")
    return parsed


def text_field(body: Mapping[str, Any], name: str) -> str:
    """A required, non-empty string field."""
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{name} is required and must be a non-empty string")
    return value.strip()


def value_field(body: Mapping[str, Any]) -> str:
    """The `value` of a setting: required, a string, and kept verbatim.

    Not stripped beyond the edges and not coerced. The stored text is what an
    operator typed, because the change record has to hold exactly that and
    the setting's own parser -- not this function -- knows what it means.
    """
    value = body.get("value")
    if not isinstance(value, str):
        raise Refused("value is required and must be a string")
    return value.strip()


def flag_field(body: Mapping[str, Any], name: str) -> bool:
    """A required boolean. Not coerced from a string.

    `read_only` decides whether a tool may change things outside CyberFriend,
    and every truthiness rule ever written gets `"false"` wrong in the
    dangerous direction. So the console has to send a real boolean.
    """
    value = body.get(name)
    if not isinstance(value, bool):
        raise Refused(f"{name} is required and must be true or false")
    return value


def account_id(body: Mapping[str, Any], name: str) -> int:
    """A platform account or channel id, as an integer.

    Accepts the string form too: JSON numbers lose precision above 2^53 and
    Discord snowflakes are 64-bit, so a console that sends them as strings is
    doing the right thing.
    """
    value = body.get(name)
    if isinstance(value, bool):  # bool is an int; "true" is not an id
        raise Refused(f"{name} must be a numeric account id")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise Refused(f"{name} must be a numeric account id")


def path_int(request: Request, name: str) -> int:
    raw = str(request.path_params[name])
    if not raw.lstrip("-").isdigit():
        raise Refused(f"{name} must be a numeric id")
    return int(raw)


def moment(value: datetime | None) -> str | None:
    """A timestamp as ISO-8601, or null. Never a locale-formatted string."""
    return None if value is None else value.isoformat()


@dataclass(frozen=True, slots=True)
class Ok:
    """A successful mutation, as the console reads it.

    Carries what changed rather than a bare 200, because the console renders
    the result instead of re-fetching -- and because a handler that has to
    name what it did cannot quietly do nothing.
    """

    changed: str
    detail: str = ""

    def response(self, **extra: Any) -> JSONResponse:
        payload: dict[str, Any] = {"changed": self.changed}
        if self.detail:
            payload["detail"] = self.detail
        payload.update(extra)
        return JSONResponse(payload)
