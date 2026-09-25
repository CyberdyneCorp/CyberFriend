"""Security headers on every console response, refusals included.

Cookie sessions bring framing and content-sniffing into scope, which bearer
tokens typed into memory did not. The CSP allows only this origin, forbids
framing by anyone, and lets forms post only here and to the issuer.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def content_security_policy(issuer: str | None) -> str:
    form_action = "'self'" + (f" {issuer}" if issuer else "")
    return (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        f"form-action {form_action}"
    )


class SecurityHeaders:
    def __init__(self, app: ASGIApp, issuer: str | None = None) -> None:
        self._app = app
        self._headers = {
            "content-security-policy": content_security_policy(issuer),
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self._headers.items():
                    headers[name] = value
            await send(message)

        await self._app(scope, receive, with_headers)
