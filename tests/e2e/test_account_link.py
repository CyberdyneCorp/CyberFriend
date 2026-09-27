"""Consent in Discord, a link on the web, the user area, and delete everything.

Both processes as they are deployed, over one real database: the bot through
FakeDiscord with a fake provisioner, and the admin process (`admin.build`)
signed in through FakeOIDC. Leo consents in a DM and gets a link; following
it in a browser and signing in with the consented, verified email links his
account; the bot DMs him about it with [Unlink]; the user area shows his own
data; and "delete everything" there, after a fresh sign-in, erases it, ends
the link and the session, and says the CyberdyneAuth account is not deleted.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import discord
import httpx
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.entrypoints import admin
from tests.e2e.harness.accounts import FakeProvisioner
from tests.e2e.harness.conversation import E2EBot, Turn
from tests.e2e.harness.oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeOIDC
from tests.e2e.harness.process import FakeClock, e2e_settings, start
from tests.e2e.harness.web import NetworkSeal
from tests.e2e.test_richer_facts import EMAIL, INTRODUCTION

KEY = "e2e-provisioning-email-key-0123456789"
PUBLIC_URL = "https://admin.e2e.test"
CSRF = {"X-CyberFriend-Console": "1"}


@dataclass
class Both:
    bot: E2EBot
    console: admin.ConsoleProcess
    fake: FakeOIDC

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.console.app), base_url=PUBLIC_URL
        )

    async def sign_in(self, browser: httpx.AsyncClient, start_url: str) -> httpx.Response:
        started = await browser.get(start_url)
        assert started.status_code == 302, started.text
        code, state = self.fake.authorize(started.headers["location"], "leo-sub")
        return await browser.get("/auth/callback", params={"code": code, "state": state})


@pytest_asyncio.fixture
async def both(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[Both]:
    settings = e2e_settings(e2e_database_url).model_copy(
        update={
            "account_provisioning_enabled": True,
            "provisioning_email_key": SecretStr(KEY),
            "admin_public_url": PUBLIC_URL,
        }
    )
    bot = await start(settings, clean, sealed_network, FakeProvisioner())
    # The admin process reads the real clock; the link code's 15 minutes run
    # on the bot's, so both start from now.
    clock = bot.process.edges.clock
    assert isinstance(clock, FakeClock)
    clock.now = datetime.now(UTC)
    fake = FakeOIDC()
    fake.add("leo-sub", EMAIL, [])
    console = admin.build(
        {
            "DATABASE_URL": e2e_database_url,
            "ADMIN_OIDC_ISSUER": ISSUER,
            "ADMIN_OIDC_CLIENT_ID": CLIENT_ID,
            "ADMIN_OIDC_CLIENT_SECRET": CLIENT_SECRET,
            "ADMIN_SESSION_KEY": base64.b64encode(bytes(range(32))).decode(),
            "ADMIN_PUBLIC_URL": PUBLIC_URL,
            "ACCOUNT_PROVISIONING_ENABLED": "true",
            "PROVISIONING_EMAIL_KEY": KEY,
        },
        transport=fake.transport,
    )
    try:
        yield Both(bot, console, fake)
    finally:
        assert console.sign_in is not None
        await console.sign_in.provider.aclose()
        await console.engine.dispose()
        federation = bot.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


async def _link_path(both: Both) -> tuple[discord.Member, str]:
    """Leo consents in a DM and asks for his sign-in link."""
    bot = both.bot
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.say(INTRODUCTION)
    [consent] = (await dm.slash("account create")).sent
    [reply] = [s for s in (await dm.press(consent, "Confirm")).sent if s.buttons]
    [link] = [s for s in (await dm.press(reply, "Link my account")).sent if PUBLIC_URL in s.text]
    url = next(word for word in link.text.split() if word.startswith(PUBLIC_URL))
    [code] = parse_qs(urlsplit(url).query)["code"]
    assert urlsplit(url).path == "/link"
    return leo, f"/link?code={code}"


async def _announce(bot: E2EBot) -> Turn:
    announcer = bot.process.link_announcer
    assert announcer is not None
    return await bot.turn(announcer.announce)


async def _count(engine: AsyncEngine, sql: str) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text(sql)) or 0)


async def test_consent_link_user_area_and_delete_everything(both: Both) -> None:
    bot = both.bot
    _, link_path = await _link_path(both)

    async with both.browser() as browser:
        linked = await both.sign_in(browser, link_path)
        assert linked.status_code == 302, linked.text
        assert linked.headers["location"] == f"{PUBLIC_URL}/#/me"
        assert both.fake.authorizations[-1]["prompt"] == "login"

        [notice] = (await _announce(bot)).sent
        assert r"l\*\*\*@gmail.com" in notice.text and "Not you?" in notice.text
        assert [label for label, _ in notice.buttons] == ["Unlink"]
        assert (await _announce(bot)).sent == (), "announced once"

        privacy = await browser.get("/me/privacy")
        assert privacy.status_code == 200, privacy.text
        facts = {f["kind"]: f["value"] for f in privacy.json()["facts"]}
        assert facts["email"] == EMAIL
        assert (await browser.get("/api/status")).status_code == 401

        suggested = await browser.post(
            "/me/feature-requests", json={"text": "a dark mode"}, headers=CSRF
        )
        assert suggested.status_code == 201, suggested.text
        mine = (await browser.get("/me/feature-requests")).json()
        assert [s["text"] for s in mine] == ["a dark mode"]

        stale = await browser.post(
            "/me/erase", json={"mode": "erase", "confirm": "DELETE"}, headers=CSRF
        )
        assert stale.status_code == 403
        fresh = await both.sign_in(browser, "/auth/user/fresh")
        assert fresh.status_code == 302, fresh.text
        erased = await browser.post(
            "/me/erase", json={"mode": "erase", "confirm": "DELETE"}, headers=CSRF
        )
        assert erased.status_code == 200, erased.text
        assert "not deleted" in erased.json()["identity_account"]
        assert (await browser.get("/me/privacy")).status_code == 401

    engine = bot.engine
    assert await _count(engine, "SELECT count(*) FROM person_account_link") == 0
    assert await _count(engine, "SELECT count(*) FROM user_session WHERE revoked_at IS NULL") == 0
    assert await _count(engine, "SELECT count(*) FROM person_fact") == 0
    assert await _count(engine, "SELECT count(*) FROM feature_request") == 0
    assert await _count(
        engine, "SELECT count(*) FROM erasure_request WHERE completed_at IS NULL"
    ) == 0
    assert both.fake.revoked, "the refresh token was revoked at CyberdyneAuth"
    assert bot.seal.refused == []


async def test_unlink_in_discord_ends_web_access(both: Both) -> None:
    bot = both.bot
    leo, link_path = await _link_path(both)

    async with both.browser() as browser:
        assert (await both.sign_in(browser, link_path)).status_code == 302
        [notice] = (await _announce(bot)).sent

        unlinked = await bot.dm(leo).press(notice, "Unlink")

        assert "Unlinked" in unlinked.text
        assert (await browser.get("/me/privacy")).status_code == 401
    assert await _count(bot.engine, "SELECT count(*) FROM person_account_link") == 0
