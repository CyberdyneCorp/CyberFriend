"""A fake CyberdyneAuth: an OIDC issuer behind an `httpx.MockTransport`.

Shaped like the real contract: discovery, a JWKS of generated RSA keys,
authorization code + PKCE (S256) with client_secret_basic, rotating refresh
tokens with reuse detection (a spent refresh token revokes the family),
userinfo, revocation and an end-session endpoint. The admin process is built
over `transport`, so a scenario signs in end to end without a network.

Knobs for the negative cases are plain attributes: overrides merged into the
next access or id token, a different `sub` for userinfo, a missing nonce.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://auth.test"
CLIENT_ID = "cyberfriend"
CLIENT_SECRET = "fake-client-secret"
AUDIENCE = "cyberfriend"
END_SESSION = f"{ISSUER}/logout"

_KEYS: list[rsa.RSAPrivateKey] = []


def _key(index: int) -> rsa.RSAPrivateKey:
    """Generated once per test session: RSA generation is the slow part."""
    while len(_KEYS) <= index:
        _KEYS.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return _KEYS[index]


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**public, "kid": kid, "use": "sig", "alg": "RS256"}


@dataclass
class FakeUser:
    sub: str
    email: str
    #: None: no roles claim at all.
    roles: list[str] | None


@dataclass
class _Code:
    sub: str
    nonce: str | None
    challenge: str
    redirect_uri: str


@dataclass
class _Refresh:
    sub: str
    family: str
    used: bool = False


@dataclass
class FakeOIDC:
    issuer: str = ISSUER
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    access_ttl: int = 900
    clock: Callable[[], float] = time.time
    users: dict[str, FakeUser] = field(default_factory=dict)
    #: Merged into every access / id token issued, for the negative cases.
    access_overrides: dict[str, Any] = field(default_factory=dict)
    id_overrides: dict[str, Any] = field(default_factory=dict)
    userinfo_sub: str | None = None
    omit_nonce: bool = False
    #: What was asked of the fake, for assertions.
    grants: list[str] = field(default_factory=list)
    jwks_fetches: int = 0
    revoked: list[str] = field(default_factory=list)
    _kid: str = "k1"
    #: Every key id ever signed with, in order; its index picks the RSA key.
    _kids: list[str] = field(default_factory=lambda: ["k1"])
    _published: list[str] = field(default_factory=lambda: ["k1"])
    _codes: dict[str, _Code] = field(default_factory=dict)
    _refresh: dict[str, _Refresh] = field(default_factory=dict)
    _dead_families: set[str] = field(default_factory=set)
    _access: dict[str, str] = field(default_factory=dict)

    # --- people ------------------------------------------------------

    def add(self, sub: str, email: str, roles: list[str] | None) -> FakeUser:
        user = FakeUser(sub, email, roles)
        self.users[sub] = user
        return user

    def role(self, name: str) -> str:
        return f"{self.client_id}:{name}"

    # --- keys --------------------------------------------------------

    def rotate_key(self, *, publish: bool = True) -> str:
        """Sign with a new key from now on; publish it unless told not to."""
        self._kid = f"k{len(self._kids) + 1}"
        self._kids.append(self._kid)
        if publish:
            self._published.append(self._kid)
        return self._kid

    def _signing_key(self, kid: str) -> rsa.RSAPrivateKey:
        return _key(self._kids.index(kid))

    def sign(self, claims: dict[str, Any], *, kid: str | None = None, **header: Any) -> str:
        kid = kid or self._kid
        return jwt.encode(
            claims,
            self._signing_key(kid),
            algorithm="RS256",
            headers={"kid": kid, **header},
        )

    def access_claims(self, sub: str, **overrides: Any) -> dict[str, Any]:
        now = int(self.clock())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": sub,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + self.access_ttl,
            "type": "access",
            "client_id": self.client_id,
        }
        roles = self.users[sub].roles if sub in self.users else None
        if roles is not None:
            claims["roles"] = list(roles)
        return {**claims, **self.access_overrides, **overrides}

    def mint_access(self, sub: str, **overrides: Any) -> str:
        return self.sign(self.access_claims(sub, **overrides))

    def id_claims(self, sub: str, nonce: str | None, **overrides: Any) -> dict[str, Any]:
        now = int(self.clock())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": sub,
            "aud": self.client_id,
            "iat": now,
            "exp": now + 3600,
            "auth_time": now,
        }
        if nonce is not None and not self.omit_nonce:
            claims["nonce"] = nonce
        return {**claims, **self.id_overrides, **overrides}

    # --- the browser's half ------------------------------------------

    def authorize(self, authorization_url: str, sub: str) -> tuple[str, str]:
        """What the login page does for `sub`: returns (code, state)."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        assert query["client_id"] == self.client_id
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self._codes[code] = _Code(
            sub=sub,
            nonce=query.get("nonce"),
            challenge=query["code_challenge"],
            redirect_uri=query["redirect_uri"],
        )
        return code, query["state"]

    # --- the transport -----------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        # A real round trip yields to the event loop; so does this one, or two
        # concurrent requests could never interleave in a test.
        await asyncio.sleep(0)
        if request.url.host != urlsplit(self.issuer).hostname:
            raise AssertionError(f"FakeOIDC asked for {request.url}")
        routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {
            ("GET", "/.well-known/openid-configuration"): self._discovery,
            ("GET", "/jwks"): self._jwks,
            ("POST", "/token"): self._token,
            ("GET", "/userinfo"): self._userinfo,
            ("POST", "/revoke"): self._revoke,
        }
        handler = routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"error": "not_found"})
        return handler(request)

    def _discovery(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "userinfo_endpoint": f"{self.issuer}/userinfo",
                "jwks_uri": f"{self.issuer}/jwks",
                "end_session_endpoint": END_SESSION,
                "revocation_endpoint": f"{self.issuer}/revoke",
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                "code_challenge_methods_supported": ["S256"],
            },
        )

    def _jwks(self, _: httpx.Request) -> httpx.Response:
        self.jwks_fetches += 1
        keys = [_jwk(self._signing_key(kid), kid) for kid in self._published]
        return httpx.Response(200, json={"keys": keys})

    def _client_ok(self, request: httpx.Request) -> bool:
        expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        presented: str = request.headers.get("authorization", "")
        return presented == f"Basic {expected}"

    def _token(self, request: httpx.Request) -> httpx.Response:
        if not self._client_ok(request):
            return httpx.Response(401, json={"error": "invalid_client"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.grants.append(form.get("grant_type", ""))
        if form.get("grant_type") == "authorization_code":
            return self._code_grant(form)
        if form.get("grant_type") == "refresh_token":
            return self._refresh_grant(form)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _code_grant(self, form: dict[str, str]) -> httpx.Response:
        code = self._codes.pop(form.get("code", ""), None)
        if code is None or form.get("redirect_uri") != code.redirect_uri:
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier = form.get("code_verifier", "")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if challenge != code.challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return self._issue(code.sub, code.nonce, family=secrets.token_hex(8))

    def _refresh_grant(self, form: dict[str, str]) -> httpx.Response:
        found = self._refresh.get(form.get("refresh_token", ""))
        if found is None or found.family in self._dead_families:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if found.used:
            # Reuse detection: the whole family is revoked.
            self._dead_families.add(found.family)
            return httpx.Response(400, json={"error": "invalid_grant"})
        found.used = True
        return self._issue(found.sub, None, family=found.family)

    def _issue(self, sub: str, nonce: str | None, *, family: str) -> httpx.Response:
        access = self.mint_access(sub)
        refresh = secrets.token_urlsafe(24)
        self._refresh[refresh] = _Refresh(sub=sub, family=family)
        self._access[access] = sub
        body: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.access_ttl,
            "refresh_token": refresh,
            "refresh_expires_in": 30 * 86400,
        }
        if nonce is not None:
            body["id_token"] = self.sign(self.id_claims(sub, nonce))
        return httpx.Response(200, json=body)

    def _userinfo(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        sub = self._access.get(token)
        if sub is None:
            return httpx.Response(401, json={"error": "invalid_token"})
        user = self.users[sub]
        return httpx.Response(
            200,
            json={"sub": self.userinfo_sub or sub, "email": user.email, "email_verified": True},
        )

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        if not self._client_ok(request):
            return httpx.Response(401, json={"error": "invalid_client"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        token = form.get("token", "")
        self.revoked.append(token)
        found = self._refresh.get(token)
        if found is not None:
            self._dead_families.add(found.family)
        return httpx.Response(200)
