"""Shared by the sign-in tests: settings, a sign-in over FakeOIDC, a browser sign-in."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from starlette.testclient import TestClient

from chatmemory.admin.oidc.config import SignInSettings
from chatmemory.admin.oidc.provider import OIDCProvider
from chatmemory.admin.oidc.service import SignIn
from chatmemory.admin.oidc.store import InMemoryLoginStore, InMemorySessionStore
from tests.e2e.harness.oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeOIDC

PUBLIC_URL = "https://admin.test"
SESSION_KEY = bytes(range(32))
SESSION_KEY_B64 = base64.b64encode(SESSION_KEY).decode()

SETTINGS = SignInSettings(
    issuer=ISSUER,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    session_key=SESSION_KEY,
    public_url=PUBLIC_URL,
)

ENVIRON = {
    "ADMIN_OIDC_ISSUER": ISSUER,
    "ADMIN_OIDC_CLIENT_ID": CLIENT_ID,
    "ADMIN_OIDC_CLIENT_SECRET": CLIENT_SECRET,
    "ADMIN_SESSION_KEY": SESSION_KEY_B64,
    "ADMIN_PUBLIC_URL": PUBLIC_URL,
}

CSRF = {"X-CyberFriend-Console": "1"}


class Clock:
    """One clock for the fake issuer and the sign-in, moved by the test."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def epoch(self) -> float:
        return self.now.timestamp()

    def advance(self, by: timedelta) -> None:
        self.now += by


@dataclass
class Rig:
    fake: FakeOIDC
    sign_in: SignIn
    logins: InMemoryLoginStore
    sessions: InMemorySessionStore
    clock: Clock


def rig() -> Rig:
    clock = Clock()
    fake = FakeOIDC(clock=clock.epoch)
    fake.add("ana-sub", "ana@cyberdyne.test", [fake.role("admin")])
    fake.add("ben-sub", "ben@cyberdyne.test", [fake.role("operator")])
    fake.add("cass-sub", "cass@cyberdyne.test", None)
    fake.add("dan-sub", "dan@cyberdyne.test", ["other-client:admin"])
    logins, sessions = InMemoryLoginStore(), InMemorySessionStore()
    provider = OIDCProvider(SETTINGS, transport=fake.transport)
    sign_in = SignIn(SETTINGS, provider, logins, sessions, clock=clock)
    return Rig(fake, sign_in, logins, sessions, clock)


def start(client: TestClient) -> str:
    """GET /auth/login; the authorization URL it redirects to."""
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 302, response.text
    return str(response.headers["location"])


def browser_sign_in(client: TestClient, fake: FakeOIDC, sub: str) -> int:
    """The whole round trip in one browser; the callback's status."""
    code, state = fake.authorize(start(client), sub)
    response = client.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )
    return response.status_code
