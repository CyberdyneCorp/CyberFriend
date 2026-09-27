"""The approved CyberdyneAuth provisioning contract, behind `AccountProvisioner`.

Two calls, both to the configured issuer:

1.  A `client_credentials` token for the provisioning client, scope
    `users:provision` only, from the token endpoint the issuer's discovery
    document names. The discovery `issuer` must be the configured one, as in
    the console's sign-in. The token is kept until shortly before it expires;
    a 401 from the endpoint drops it and the call is tried once more.
2.  `POST {issuer}/api/v1/users/provision` with `{email, name?, locale?}` --
    exactly the fields of `ProvisioningRequest`, absent ones left out.

The answers the contract allows:

*   202 -- accepted, whatever CyberdyneAuth did with it. The body carries
    nothing to learn, so it is not read.
*   429 -- the per-client limit: `ProvisioningRateLimited`.
*   422 -- the name was refused: `ProvisioningInvalidName`.
*   anything else, or no answer -- `ProvisioningUnavailable`.

Every request goes through the injected transport (the bot's
`Edges.http_transport`), so tests put the contract behind an
`httpx.MockTransport` and the network is never reached. Nothing here logs the
email, the name or a token: only statuses.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from chatmemory.ports.accounts import (
    ProvisioningInvalidName,
    ProvisioningRateLimited,
    ProvisioningRequest,
    ProvisioningUnavailable,
)

log = structlog.get_logger()

DISCOVERY_PATH = "/.well-known/openid-configuration"
PROVISION_PATH = "/api/v1/users/provision"
SCOPE = "users:provision"
TIMEOUT_SECONDS = 10.0
TOKEN_EXPIRY_MARGIN_SECONDS = 30.0
"""A token this close to expiring is renewed rather than sent."""
DEFAULT_TOKEN_LIFETIME_SECONDS = 60.0
"""Assumed when the token response carries no `expires_in`."""


@dataclass(frozen=True, slots=True)
class ProvisioningClient:
    """The provisioning client: not the console's sign-in client."""

    issuer: str
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Endpoint:
    token_endpoint: str
    auth_methods: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Token:
    value: str = field(repr=False)
    expires_at: float


class CyberdyneAuthProvisioner:
    """`AccountProvisioner` over the approved CyberdyneAuth contract."""

    def __init__(
        self,
        client: ProvisioningClient,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._issuer = client.issuer.rstrip("/")
        self._transport = transport
        self._timeout = timeout_seconds
        self._monotonic = monotonic
        self._endpoint: _Endpoint | None = None
        self._token: _Token | None = None

    async def request_account(self, request: ProvisioningRequest) -> None:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
            try:
                response = await self._provision(http, request)
                if response.status_code == httpx.codes.UNAUTHORIZED:
                    # Revoked or rotated early: one fresh token, then the answer stands.
                    self._token = None
                    response = await self._provision(http, request)
            except httpx.HTTPError as exc:
                raise ProvisioningUnavailable("provisioning endpoint unreachable") from exc
        _raise_unless_accepted(response.status_code)

    async def _provision(
        self, http: httpx.AsyncClient, request: ProvisioningRequest
    ) -> httpx.Response:
        token = await self._access_token(http)
        return await http.post(
            self._issuer + PROVISION_PATH,
            json=provision_body(request),
            headers={"Authorization": f"Bearer {token}"},
        )

    # --- the client_credentials token ---------------------------------

    async def _access_token(self, http: httpx.AsyncClient) -> str:
        now = self._monotonic()
        if self._token is not None and self._token.expires_at - TOKEN_EXPIRY_MARGIN_SECONDS > now:
            return self._token.value
        endpoint = await self._discovery(http)
        data, headers = _client_auth(
            {"grant_type": "client_credentials", "scope": SCOPE},
            self._client,
            endpoint.auth_methods,
        )
        response = await http.post(endpoint.token_endpoint, data=data, headers=headers)
        if response.status_code != httpx.codes.OK:
            log.warning("accounts.provisioning_token_refused", status=response.status_code)
            raise ProvisioningUnavailable("token request refused")
        self._token = _bearer(_json(response), now)
        return self._token.value

    async def _discovery(self, http: httpx.AsyncClient) -> _Endpoint:
        if self._endpoint is not None:
            return self._endpoint
        response = await http.get(self._issuer + DISCOVERY_PATH)
        doc = _json(response) if response.status_code == httpx.codes.OK else {}
        issuer, token_endpoint = doc.get("issuer"), doc.get("token_endpoint")
        if not isinstance(issuer, str) or issuer.rstrip("/") != self._issuer:
            raise ProvisioningUnavailable("discovery names a different issuer")
        if not isinstance(token_endpoint, str) or not token_endpoint:
            raise ProvisioningUnavailable("discovery names no token endpoint")
        methods = doc.get("token_endpoint_auth_methods_supported")
        self._endpoint = _Endpoint(
            token_endpoint=token_endpoint,
            auth_methods=tuple(m for m in methods if isinstance(m, str))
            if isinstance(methods, list)
            else (),
        )
        return self._endpoint


def provision_body(request: ProvisioningRequest) -> dict[str, str]:
    """`{email, name?, locale?}`: the request's fields, absent ones left out."""
    body = {"email": request.email}
    if request.name:
        body["name"] = request.name
    if request.locale:
        body["locale"] = request.locale
    return body


def _raise_unless_accepted(status: int) -> None:
    if status == httpx.codes.ACCEPTED:
        return
    log.info("accounts.provisioning_answer", status=status)
    if status == httpx.codes.TOO_MANY_REQUESTS:
        raise ProvisioningRateLimited("per-client limit")
    if status == httpx.codes.UNPROCESSABLE_ENTITY:
        raise ProvisioningInvalidName("name refused")
    raise ProvisioningUnavailable(f"unexpected status {status}")


def _client_auth(
    form: dict[str, str], client: ProvisioningClient, methods: tuple[str, ...]
) -> tuple[dict[str, str], dict[str, str]]:
    """client_secret_basic unless the issuer offers only client_secret_post."""
    if methods and "client_secret_basic" not in methods and "client_secret_post" in methods:
        return {**form, "client_id": client.client_id, "client_secret": client.client_secret}, {}
    # RFC 6749 2.3.1: form-encode each half before the Basic encoding.
    pair = f"{quote(client.client_id, safe='')}:{quote(client.client_secret, safe='')}"
    return form, {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}


def _bearer(body: Mapping[str, Any], now: float) -> _Token:
    token, token_type = body.get("access_token"), body.get("token_type")
    if not isinstance(token, str) or not token:
        raise ProvisioningUnavailable("token response has no access token")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise ProvisioningUnavailable("token response is not a bearer token")
    lifetime = body.get("expires_in")
    seconds = (
        float(lifetime)
        if isinstance(lifetime, int | float) and not isinstance(lifetime, bool)
        else DEFAULT_TOKEN_LIFETIME_SECONDS
    )
    return _Token(value=token, expires_at=now + seconds)


def _json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
