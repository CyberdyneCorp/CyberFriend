"""The admin process, signed in through a fake CyberdyneAuth, over the real database.

`entrypoints.admin.build` is what the container runs; only the transport is
the fake issuer's. An operator reads and cannot change anything, an admin's
change is recorded against `oidc:<sub>` with their email, and signing out
ends the session at once.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.entrypoints import admin
from tests.e2e.harness.oidc import CLIENT_ID, CLIENT_SECRET, END_SESSION, ISSUER, FakeOIDC

PUBLIC_URL = "https://admin.test"
CSRF = {"X-CyberFriend-Console": "1"}


def _environ(database_url: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "ADMIN_OIDC_ISSUER": ISSUER,
        "ADMIN_OIDC_CLIENT_ID": CLIENT_ID,
        "ADMIN_OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "ADMIN_SESSION_KEY": base64.b64encode(bytes(range(32))).decode(),
        "ADMIN_PUBLIC_URL": PUBLIC_URL,
    }


class Console:
    def __init__(self, process: admin.ConsoleProcess, fake: FakeOIDC) -> None:
        self.process = process
        self.fake = fake

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.process.app), base_url=PUBLIC_URL
        )

    async def sign_in(self, browser: httpx.AsyncClient, sub: str) -> None:
        started = await browser.get("/auth/login")
        assert started.status_code == 302, started.text
        code, state = self.fake.authorize(started.headers["location"], sub)
        done = await browser.get("/auth/callback", params={"code": code, "state": state})
        assert done.status_code == 302, done.text
        assert done.headers["location"] == f"{PUBLIC_URL}/#/status"


@pytest_asyncio.fixture
async def console(clean: AsyncEngine, e2e_database_url: str) -> AsyncIterator[Console]:
    fake = FakeOIDC()
    fake.add("ana-sub", "ana@cyberdyne.test", [fake.role("admin")])
    fake.add("ben-sub", "ben@cyberdyne.test", [fake.role("operator")])
    process = admin.build(_environ(e2e_database_url), transport=fake.transport)
    try:
        yield Console(process, fake)
    finally:
        assert process.sign_in is not None
        await process.sign_in.provider.aclose()
        await process.engine.dispose()


async def test_an_operator_signed_in_reads_and_changes_nothing(console: Console) -> None:
    async with console.browser() as ben:
        await console.sign_in(ben, "ben-sub")

        session = (await ben.get("/api/session")).json()
        status = await ben.get("/api/status")
        change = await ben.put(
            "/api/settings/ask_min_confidence", json={"value": "0.7"}, headers=CSRF
        )

    assert session["roles"] == ["operator"] and session["via"] == "oidc"
    assert status.status_code == 200
    assert change.status_code == 403
    assert change.json() == {"error": "requires admin"}


async def test_an_admin_change_is_recorded_against_the_person(
    console: Console, clean: AsyncEngine
) -> None:
    async with console.browser() as ana:
        await console.sign_in(ana, "ana-sub")

        change = await ana.put(
            "/api/settings/ask_min_confidence", json={"value": "0.7"}, headers=CSRF
        )
        audit = (await ana.get("/api/audit")).json()

    assert change.status_code == 200, change.text
    assert (audit[0]["operator"], audit[0]["operator_display"]) == (
        "oidc:ana-sub",
        "ana@cyberdyne.test",
    )
    async with clean.connect() as conn:
        stored = (await conn.execute(text("SELECT sub, email FROM admin_session"))).one()
        leaked = await conn.scalar(
            text(
                "SELECT count(*) FROM admin_session "
                "WHERE position('eyJ'::bytea in access_token_enc) > 0"
            )
        )
    assert tuple(stored) == ("ana-sub", "ana@cyberdyne.test")
    assert leaked == 0


async def test_signing_out_ends_the_session(console: Console) -> None:
    async with console.browser() as ana:
        await console.sign_in(ana, "ana-sub")
        assert (await ana.get("/api/status")).status_code == 200
        cookie = ana.cookies.get("__Host-cf_admin")

        out = await ana.post("/auth/logout", headers=CSRF)
        after = await ana.get("/api/status")

    assert out.status_code == 200
    assert out.json()["end_session_url"].startswith(END_SESSION + "?")
    assert after.status_code == 401
    assert console.fake.revoked, "the refresh token was not revoked at the issuer"
    async with console.browser() as replay:
        replayed = await replay.get(
            "/api/status", headers={"Cookie": f"__Host-cf_admin={cookie}"}
        )
    assert replayed.status_code == 401
