"""`/link`, `/auth/user/*` and `/me/*`: the web user area's routes.

`/link`, `/auth/user/login` and `/auth/user/fresh` are public rows: each starts
a user sign-in (see `service`). Every `/me` route is a `user` row, reached
only with the user cookie (`auth`), and finds the person from the signed-in
subject's link and nothing else: no route reads a person or platform id from
the request. An unlinked subject and an unknown one get the same answer.

With the user area off every route answers 404, as `/auth/*` does with
sign-in off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from chatmemory.admin.auth import LOGIN_COOKIE, cookie_value
from chatmemory.admin.oidc.service import LOGIN_LIFETIME, SignInFailed
from chatmemory.admin.oidc.store import LoginRecord
from chatmemory.admin.user.auth import USER_COOKIE, current_user
from chatmemory.admin.user.service import (
    FRESH_PURPOSE,
    LINK_PURPOSE,
    USER_PURPOSE,
    LinkFailure,
    LinkRefused,
    UserSignIn,
)
from chatmemory.app.accounts import code_digest
from chatmemory.app.feature_requests import FeatureRequestService, SubmitOutcome
from chatmemory.app.language import Language
from chatmemory.app.privacy import PrivacyReport, PrivacyService
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.feature_requests import SourceKind, SuggestionSource
from chatmemory.ports.privacy import ErasureMode, Inventory

NO_STORE = {"Cache-Control": "no-store"}

NOT_LINKED = (
    "No CyberFriend profile is linked to this account. "
    "Link one from Discord with /account."
)
ACCOUNT_NOT_DELETED = (
    "Your CyberdyneAuth account, with its name and email, is not deleted: "
    "CyberdyneAuth cannot delete accounts on request yet. To have it deleted, "
    "ask a server admin to request its deletion from the CyberdyneAuth team."
)
CONFIRM_WORD = "DELETE"
MAX_CODE_CHARS = 128
"""Link codes are 43 characters; anything far longer is not one."""

ME_HOME = "/#/me"

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>CyberFriend</title></head>
<body><h1>{heading}</h1><p>{body}</p><p><a href="{home}">Continue</a></p></body>
</html>"""

LINK_PAGES: dict[LinkFailure, tuple[str, str]] = {
    LinkFailure.FAILED: (
        "The link did not work.",
        "A link works once, for 15 minutes, in the browser that opened it. "
        "Ask for a new one in Discord with /account link.",
    ),
    LinkFailure.UNVERIFIED: (
        "Your email is not verified yet.",
        "Accept the invitation CyberdyneAuth emailed you, then ask for a new "
        "link in Discord with /account link.",
    ),
    LinkFailure.REFUSED: (
        "This account cannot be linked.",
        "Sign in with the email you gave in Discord. If this CyberdyneAuth "
        "account is linked to another Discord account, unlink it there first.",
    ),
}
SIGN_IN_FAILED = (
    "Sign-in did not complete.",
    "Start again. A sign-in link works once, in the browser that opened it.",
)

SUBMIT_MESSAGES: dict[SubmitOutcome, str] = {
    SubmitOutcome.RECORDED: "Thanks, your suggestion was recorded.",
    SubmitOutcome.ALREADY_RECORDED: "You already suggested this; it is recorded once.",
    SubmitOutcome.EMPTY: "The suggestion is empty.",
    SubmitOutcome.TOO_LONG: "A suggestion can be at most 1000 characters.",
    SubmitOutcome.CONTAINS_CONTACT: (
        "Suggestions can't include an email address, a phone number, a wallet "
        "address or other contact details. Please remove it and try again."
    ),
    SubmitOutcome.LIMITED: "You've made 5 suggestions in the last 24 hours. Try again later.",
    SubmitOutcome.OPTED_OUT: (
        "You asked me to stop archiving you, so suggestions aren't stored."
    ),
}

SUBMIT_STATUS = {
    SubmitOutcome.RECORDED: 201,
    SubmitOutcome.ALREADY_RECORDED: 200,
    SubmitOutcome.LIMITED: 429,
}
"""Every other outcome is a refusal of the text itself: 422."""


@dataclass(frozen=True, slots=True)
class UserArea:
    """What the user area is wired to. No defaults: half-wired fails to build."""

    sign_in: UserSignIn
    privacy: PrivacyService
    suggestions: FeatureRequestService


def routes(area: UserArea | None) -> list[Route]:
    return [
        Route("/link", _begin(area, LINK_PURPOSE), methods=["GET"], name="user_link"),
        Route("/auth/user/login", _begin(area, USER_PURPOSE), methods=["GET"], name="user_login"),
        Route("/auth/user/fresh", _begin(area, FRESH_PURPOSE), methods=["GET"], name="user_fresh"),
        Route("/me/session", _me(area, _session), methods=["GET"], name="me_session"),
        Route("/me/privacy", _me(area, _privacy), methods=["GET"], name="me_privacy"),
        Route(
            "/me/feature-requests",
            _me(area, _suggestions),
            methods=["GET", "POST"],
            name="me_feature_requests",
        ),
        Route("/me/erase", _me(area, _erase), methods=["POST"], name="me_erase"),
        Route("/me/logout", _me(area, _logout), methods=["POST"], name="me_logout"),
    ]


# --- starting a sign-in --------------------------------------------------------


def _begin(area: UserArea | None, purpose: str) -> Any:
    async def begin(request: Request) -> Response:
        if area is None:
            return _not_found()
        code_sha256 = None
        if purpose == LINK_PURPOSE:
            code = request.query_params.get("code", "")
            if not code or len(code) > MAX_CODE_CHARS:
                return _page(LINK_PAGES[LinkFailure.FAILED], 400)
            code_sha256 = code_digest(code)
        begun = await area.sign_in.begin(purpose, code_sha256=code_sha256)
        response = RedirectResponse(begun.authorization_url, status_code=302, headers=NO_STORE)
        response.set_cookie(
            LOGIN_COOKIE,
            begun.binding,
            max_age=int(LOGIN_LIFETIME.total_seconds()),
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    return begin


async def user_callback(area: UserArea, login: LoginRecord, request: Request) -> Response:
    """`/auth/callback` for a login a user sign-in started."""
    result = await area.sign_in.complete(
        login,
        code=request.query_params.get("code"),
        binding=cookie_value(request.scope, LOGIN_COOKIE),
    )
    response: Response
    if isinstance(result, SignInFailed):
        response = _page(SIGN_IN_FAILED, 400)
    elif isinstance(result, LinkRefused):
        response = _page(LINK_PAGES[result.failure], 403)
    else:
        home = area.sign_in.sign_in.settings.public_url + ME_HOME
        response = RedirectResponse(home, status_code=302, headers=NO_STORE)
        response.set_cookie(
            USER_COOKIE,
            result.session_id,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
    response.delete_cookie(LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


# --- /me ------------------------------------------------------------------------


def _me(area: UserArea | None, handler: Any) -> Any:
    async def endpoint(request: Request) -> Response:
        if area is None:
            return _not_found()
        response: Response = await handler(area, request)
        response.headers.update(NO_STORE)
        return response

    return endpoint


async def _person(area: UserArea) -> PersonRef | None:
    return await area.sign_in.linking.person_for(current_user().sub)


def _not_linked() -> JSONResponse:
    return JSONResponse({"error": NOT_LINKED}, status_code=404)


async def _session(area: UserArea, _: Request) -> Response:
    user = current_user()
    person = await _person(area)
    return JSONResponse(
        {
            "email": user.email,
            "linked": person is not None,
            "fresh": user.fresh(area.sign_in.now()),
        }
    )


async def _privacy(area: UserArea, _: Request) -> Response:
    person = await _person(area)
    if person is None:
        return _not_linked()
    report = await area.privacy.report(person)
    return JSONResponse(privacy_json(report))


async def _suggestions(area: UserArea, request: Request) -> Response:
    person = await _person(area)
    if person is None:
        return _not_linked()
    if request.method == "GET":
        mine = await area.suggestions.list_own(person)
        return JSONResponse(
            [
                {
                    "id": s.id,
                    "text": s.text,
                    "status": s.status.value,
                    "created_at": s.created_at.isoformat(),
                }
                for s in mine
            ]
        )
    body = await _json_body(request)
    text = body.get("text") if body is not None else None
    if not isinstance(text, str):
        return JSONResponse({"error": "send {\"text\": \"...\"}"}, status_code=400)
    result = await area.suggestions.submit(
        person,
        text,
        SuggestionSource(kind=SourceKind.WEB, platform="web"),
        language=Language.ENGLISH,
    )
    return JSONResponse(
        {
            "outcome": result.outcome.value,
            "id": result.request_id,
            "message": SUBMIT_MESSAGES[result.outcome],
        },
        status_code=SUBMIT_STATUS.get(result.outcome, 422),
    )


async def _erase(area: UserArea, request: Request) -> Response:
    user = current_user()
    person = await _person(area)
    if person is None:
        return _not_linked()
    body = await _json_body(request) or {}
    try:
        mode = ErasureMode(str(body.get("mode")))
    except ValueError:
        return JSONResponse(
            {"error": "mode must be 'erase' or 'erase_and_opt_out'"}, status_code=400
        )
    if body.get("confirm") != CONFIRM_WORD:
        return JSONResponse({"error": f"type {CONFIRM_WORD} to confirm"}, status_code=400)
    if not user.fresh(area.sign_in.now()):
        return JSONResponse(
            {"error": "sign in again first: deleting everything needs a sign-in "
             "from the last five minutes", "reauth": "/auth/user/fresh"},
            status_code=403,
        )
    # Read before the erasure: its purge deletes this session's row.
    refresh = await area.sign_in.refresh_token_of(user)
    erased = await area.privacy.erase(person, mode)
    await area.sign_in.end_subject(user.sub, refresh)
    response = JSONResponse(
        {
            "mode": erased.mode.value,
            "counts": erased.counts.as_json(),
            "identity_account": ACCOUNT_NOT_DELETED,
        }
    )
    _clear(response)
    return response


async def _logout(area: UserArea, request: Request) -> Response:
    await area.sign_in.sign_out(cookie_value(request.scope, USER_COOKIE))
    response = JSONResponse({"signed_out": True})
    _clear(response)
    return response


# --- helpers ----------------------------------------------------------------------


def privacy_json(report: PrivacyReport) -> dict[str, Any]:
    """The DM-level inventory, and what the deployment keeps and for how long."""
    inventory: Inventory = report.inventory
    retention = report.retention
    return {
        "known": inventory.known,
        "archiving": inventory.archiving,
        "platforms": list(inventory.platforms),
        "facts": [{"kind": f.kind.value, "value": f.value} for f in inventory.facts],
        "memory": {
            "direct_turns": inventory.memory.direct_turns,
            "direct_summaries": inventory.memory.direct_summaries,
            "channel_turns": inventory.memory.channel_turns,
            "channel_summaries": inventory.memory.channel_summaries,
        },
        "recent_questions": list(inventory.recent_questions),
        "tasks": [
            {
                "id": t.id,
                "question": t.question,
                "interval_hours": t.interval_hours,
                "disabled": t.disabled,
            }
            for t in inventory.tasks
        ],
        "alerts": [
            {"id": a.id, "kind": a.kind, "chain": a.chain, "address": a.address, "asset": a.asset}
            for a in inventory.alerts
        ],
        "notifications": {
            "enabled": inventory.notifications.enabled,
            "queued": inventory.notifications.queued,
        },
        "voice_seconds_this_month": inventory.voice_seconds_this_month,
        "media": {
            "by_kind": dict(inventory.media.by_kind),
            "with_text": inventory.media.with_text,
        },
        "suggestions": [
            {"id": s.id, "text": s.text, "status": s.status} for s in inventory.suggestions
        ],
        "tokens": [
            {"label": t.label, "issued_at": t.issued_at.isoformat()} for t in inventory.tokens
        ],
        "traces": inventory.traces,
        "retention": {
            "tracing": retention.tracing,
            "trace_retention_days": retention.trace_retention_days,
            "memory_retention_days": retention.memory_retention_days,
            # None: no database backups are kept.
            "backup_retention_days": retention.backup_retention_days,
        },
        "identity_account": ACCOUNT_NOT_DELETED,
    }


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _clear(response: Response) -> None:
    response.delete_cookie(USER_COOKIE, path="/", secure=True, httponly=True, samesite="strict")


def _page(text: tuple[str, str], status: int) -> HTMLResponse:
    heading, body = text
    return HTMLResponse(
        _PAGE.format(heading=heading, body=body, home=ME_HOME), status_code=status, headers=NO_STORE
    )


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "the user area is not enabled"}, status_code=404)
