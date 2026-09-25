"""`/auth/login`, `/auth/callback`, `/auth/logout` and `GET /api/session`.

The `/auth/*` routes are public rows in the route table: they are how a
credential is obtained. With sign-in off they answer 404, so a deploy without
CyberdyneAuth behaves exactly as before. Their pages say as little as
possible -- a failed sign-in is one fixed page whatever failed, and "no
access" does not say whether the account exists.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from chatmemory.admin.auth import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    cookie_value,
    csrf_header_present,
    current_principal,
    origin_allowed,
)
from chatmemory.admin.oidc.service import (
    LOGIN_LIFETIME,
    NoAccess,
    SignedIn,
    SignIn,
)

NO_STORE = {"Cache-Control": "no-store"}

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>CyberFriend console</title></head>
<body><h1>{heading}</h1><p>{body}</p><p><a href="{home}">Back to the console</a></p></body>
</html>"""

SIGN_IN_FAILED = (
    "Sign-in did not complete.",
    "Start again from the console. A sign-in link works once, in the browser that opened it.",
)
NO_ACCESS = ("No access.", "This account has no access to the CyberFriend console.")


def routes(sign_in: SignIn | None) -> list[Route]:
    async def login(_: Request) -> Response:
        if sign_in is None:
            return _not_found()
        begun = await sign_in.begin()
        response = RedirectResponse(begun.authorization_url, status_code=302, headers=NO_STORE)
        response.set_cookie(
            LOGIN_COOKIE,
            begun.binding,
            max_age=int(LOGIN_LIFETIME.total_seconds()),
            path="/",
            secure=True,
            httponly=True,
            # Lax, not Strict: the callback is a cross-site top-level
            # navigation from the issuer, and a Strict cookie is not sent.
            samesite="lax",
        )
        return response

    async def callback(request: Request) -> Response:
        if sign_in is None:
            return _not_found()
        result = await sign_in.complete(
            state=request.query_params.get("state"),
            code=request.query_params.get("code"),
            binding=cookie_value(request.scope, LOGIN_COOKIE),
        )
        home = sign_in.settings.public_url + "/"
        response: Response
        if isinstance(result, SignedIn):
            response = RedirectResponse(
                sign_in.settings.console_url, status_code=302, headers=NO_STORE
            )
            response.set_cookie(
                SESSION_COOKIE,
                result.session_id,
                path="/",
                secure=True,
                httponly=True,
                samesite="strict",
            )
        elif isinstance(result, NoAccess):
            response = _page(NO_ACCESS, home, 403)
        else:
            response = _page(SIGN_IN_FAILED, home, 400)
        response.delete_cookie(LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    async def logout(request: Request) -> Response:
        if sign_in is None:
            return _not_found()
        public_origin = sign_in.settings.public_origin
        if not csrf_header_present(request.scope) or not origin_allowed(
            request.scope, public_origin
        ):
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        end_session = await sign_in.sign_out(cookie_value(request.scope, SESSION_COOKIE))
        response = JSONResponse({"end_session_url": end_session}, headers=NO_STORE)
        response.delete_cookie(
            SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
        )
        return response

    async def session(_: Request) -> JSONResponse:
        principal = current_principal()
        return JSONResponse(
            {
                "subject": principal.subject,
                "display": principal.display,
                "roles": sorted(role.value for role in principal.roles),
                "via": principal.via,
            },
            headers=NO_STORE,
        )

    return [
        Route("/auth/login", login, methods=["GET"], name="auth_login"),
        Route("/auth/callback", callback, methods=["GET"], name="auth_callback"),
        Route("/auth/logout", logout, methods=["POST"], name="auth_logout"),
        Route("/api/session", session, methods=["GET"], name="session"),
    ]


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "sign-in is not configured"}, status_code=404)


def _page(text: tuple[str, str], home: str, status: int) -> HTMLResponse:
    heading, body = text
    return HTMLResponse(
        _PAGE.format(heading=heading, body=body, home=home), status_code=status, headers=NO_STORE
    )
