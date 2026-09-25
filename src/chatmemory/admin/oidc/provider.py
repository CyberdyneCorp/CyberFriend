"""CyberdyneAuth over HTTP: discovery, keys, tokens, userinfo, revocation.

Every outbound call goes through one `httpx.AsyncClient` built over an
injected transport -- `entrypoints.admin.build(transport=...)`, like the bot's
`Edges.http_transport` -- so tests and the end-to-end harness put a fake
issuer behind it and the network is never reached.

Discovery is fetched once and cached. Its `issuer` must be the configured one
(OIDC Discovery 4.3), and it is discovery's value that tokens are checked
against. The key set is cached by key id and refetched when an unknown `kid`
arrives, at most once a minute, so a token with a made-up `kid` cannot make
the console hammer the issuer.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt
import structlog

from chatmemory.admin.oidc.config import SignInSettings

log = structlog.get_logger()

DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_MIN_INTERVAL_SECONDS = 60.0
TIMEOUT_SECONDS = 10.0


class ProviderUnavailable(Exception):
    """CyberdyneAuth could not be reached, or answered with something unusable."""


class TokenRequestFailed(Exception):
    """The token endpoint refused a grant. The message is its `error` code only."""


@dataclass(frozen=True, slots=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None
    revocation_endpoint: str | None
    token_auth_methods: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    id_token: str | None
    refresh_token: str | None
    refresh_expires_in: int | None


class OIDCProvider:
    """The one object in the console that talks to CyberdyneAuth."""

    def __init__(
        self,
        settings: SignInSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS)
        self._monotonic = monotonic
        self._discovery: Discovery | None = None
        self._discovery_lock = asyncio.Lock()
        self._keys: dict[str, Any] = {}
        self._keys_fetched_at: float | None = None
        self._keys_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- discovery and keys ------------------------------------------

    async def discovery(self) -> Discovery:
        if self._discovery is not None:
            return self._discovery
        async with self._discovery_lock:
            if self._discovery is None:
                self._discovery = await self._fetch_discovery()
        return self._discovery

    async def issuer(self) -> str:
        return (await self.discovery()).issuer

    async def key_for(self, kid: str | None) -> Any | None:
        key = self._cached_key(kid)
        if key is not None:
            return key
        async with self._keys_lock:
            key = self._cached_key(kid)
            if key is None and self._may_refetch_keys():
                await self._fetch_keys()
                key = self._cached_key(kid)
        if key is None:
            log.info("admin.oidc.unknown_kid", kid=kid)
        return key

    def _cached_key(self, kid: str | None) -> Any | None:
        if kid is None:
            # No `kid`: acceptable only when there is exactly one key to mean.
            return next(iter(self._keys.values())) if len(self._keys) == 1 else None
        return self._keys.get(kid)

    def _may_refetch_keys(self) -> bool:
        if self._keys_fetched_at is None:
            return True
        return self._monotonic() - self._keys_fetched_at >= JWKS_MIN_INTERVAL_SECONDS

    async def _fetch_discovery(self) -> Discovery:
        url = self._settings.issuer.rstrip("/") + DISCOVERY_PATH
        doc = await self._get_json(url)
        issuer = _text(doc, "issuer")
        if issuer.rstrip("/") != self._settings.issuer.rstrip("/"):
            raise ProviderUnavailable("discovery names a different issuer")
        methods = doc.get("token_endpoint_auth_methods_supported")
        return Discovery(
            issuer=issuer,
            authorization_endpoint=_text(doc, "authorization_endpoint"),
            token_endpoint=_text(doc, "token_endpoint"),
            userinfo_endpoint=_text(doc, "userinfo_endpoint"),
            jwks_uri=_text(doc, "jwks_uri"),
            end_session_endpoint=_optional_text(doc, "end_session_endpoint"),
            revocation_endpoint=_optional_text(doc, "revocation_endpoint"),
            token_auth_methods=tuple(m for m in methods if isinstance(m, str))
            if isinstance(methods, list)
            else (),
        )

    async def _fetch_keys(self) -> None:
        doc = await self._get_json((await self.discovery()).jwks_uri)
        self._keys_fetched_at = self._monotonic()
        entries = doc.get("keys")
        self._keys = dict(_signing_keys(entries if isinstance(entries, list) else []))

    # --- the flow ----------------------------------------------------

    async def authorization_url(
        self, *, state: str, nonce: str, challenge: str, max_age: int | None = None
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": self._settings.redirect_uri,
            "scope": self._settings.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if max_age is not None:
            params["max_age"] = str(max_age)
        return _with_query((await self.discovery()).authorization_endpoint, params)

    async def exchange_code(self, code: str, verifier: str) -> TokenResponse:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.redirect_uri,
                "code_verifier": verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenResponse:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def userinfo(self, access_token: str) -> Mapping[str, Any]:
        endpoint = (await self.discovery()).userinfo_endpoint
        return await self._get_json(endpoint, headers={"Authorization": f"Bearer {access_token}"})

    async def revoke(self, refresh_token: str) -> None:
        """Best effort: the session is already revoked on our side."""
        try:
            endpoint = (await self.discovery()).revocation_endpoint
            if endpoint is None:
                return
            data, headers = self._client_auth(
                {"token": refresh_token, "token_type_hint": "refresh_token"}
            )
            await self._http.post(endpoint, data=data, headers=headers)
        except (httpx.HTTPError, ProviderUnavailable) as exc:
            log.warning("admin.oidc.revoke_failed", error=type(exc).__name__)

    async def end_session_url(self, id_token_hint: str | None) -> str | None:
        try:
            endpoint = (await self.discovery()).end_session_endpoint
        except ProviderUnavailable:
            return None
        if endpoint is None:
            return None
        params = {
            "client_id": self._settings.client_id,
            "post_logout_redirect_uri": self._settings.post_logout_redirect_uri,
        }
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        return _with_query(endpoint, params)

    # --- HTTP --------------------------------------------------------

    async def _token_request(self, form: dict[str, str]) -> TokenResponse:
        endpoint = (await self.discovery()).token_endpoint
        data, headers = self._client_auth(form)
        try:
            response = await self._http.post(endpoint, data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("token endpoint unreachable") from exc
        body = _json(response)
        if response.status_code != 200:
            error = body.get("error") if isinstance(body.get("error"), str) else "refused"
            raise TokenRequestFailed(str(error))
        token_type = body.get("token_type")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise ProviderUnavailable("token response is not a bearer token")
        refresh_expires = body.get("refresh_expires_in")
        return TokenResponse(
            access_token=_text(body, "access_token"),
            id_token=_optional_text(body, "id_token"),
            refresh_token=_optional_text(body, "refresh_token"),
            refresh_expires_in=refresh_expires if isinstance(refresh_expires, int) else None,
        )

    def _client_auth(self, form: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """client_secret_basic unless the issuer offers only client_secret_post."""
        client_id, secret = self._settings.client_id, self._settings.client_secret
        methods = self._discovery.token_auth_methods if self._discovery else ()
        if methods and "client_secret_basic" not in methods and "client_secret_post" in methods:
            return {**form, "client_id": client_id, "client_secret": secret}, {}
        # RFC 6749 2.3.1: form-encode each half before the Basic encoding.
        pair = f"{quote(client_id, safe='')}:{quote(secret, safe='')}"
        return form, {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}

    async def _get_json(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
        try:
            response = await self._http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{httpx.URL(url).path} unreachable") from exc
        if response.status_code != 200:
            raise ProviderUnavailable(f"{httpx.URL(url).path} answered {response.status_code}")
        return _json(response)


def _signing_keys(entries: list[Any]) -> list[tuple[str, Any]]:
    keys: list[tuple[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("kty") != "RSA":
            continue
        if entry.get("use", "sig") != "sig" or entry.get("alg", "RS256") != "RS256":
            continue
        try:
            key = jwt.PyJWK(entry).key
        except jwt.PyJWTError:
            continue
        kid = entry.get("kid")
        keys.append((kid if isinstance(kid, str) else f"#{index}", key))
    return keys


def _json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderUnavailable("answer is not JSON") from exc
    if not isinstance(body, dict):
        raise ProviderUnavailable("answer is not a JSON object")
    return body


def _text(doc: Mapping[str, Any], name: str) -> str:
    value = doc.get(name)
    if not isinstance(value, str) or not value:
        raise ProviderUnavailable(f"{name} missing")
    return value


def _optional_text(doc: Mapping[str, Any], name: str) -> str | None:
    value = doc.get(name)
    return value if isinstance(value, str) and value else None


def _with_query(endpoint: str, params: Mapping[str, str]) -> str:
    separator = "&" if "?" in endpoint else "?"
    return endpoint + separator + urlencode(params)
