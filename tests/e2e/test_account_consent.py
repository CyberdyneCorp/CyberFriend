"""`/account create|link` through the assembled process, a fake provisioner and real Postgres.

Asked in a channel, the consent continues in a DM showing the exact name,
email and language, what the invitation means and that the account outlives
Delete everything. Only [Confirm] sends anything, and what is sent is those
three values, whatever else the person has on file. A second request within a
day is refused and nothing is sent. [Link my account] DMs a single-use link
whose code is stored only as a hash. Without an email fact the address is
typed into a form, and it is not saved as a fact.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.app.accounts import email_hmac
from chatmemory.ports.accounts import ProvisioningRequest
from tests.e2e.harness.accounts import FakeProvisioner
from tests.e2e.harness.conversation import Conversation, E2EBot
from tests.e2e.harness.discord_wire import Sent
from tests.e2e.harness.process import e2e_settings, start
from tests.e2e.harness.web import NetworkSeal
from tests.e2e.test_richer_facts import EMAIL, INTRODUCTION

KEY = "e2e-provisioning-email-key-0123456789"
CONSOLE = "https://console.e2e.test"


@pytest_asyncio.fixture
async def accounts(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[tuple[E2EBot, FakeProvisioner]]:
    settings = e2e_settings(e2e_database_url).model_copy(
        update={
            "account_provisioning_enabled": True,
            "provisioning_email_key": SecretStr(KEY),
            "admin_public_url": CONSOLE,
        }
    )
    provisioner = FakeProvisioner()
    e2e = await start(settings, clean, sealed_network, provisioner)
    try:
        yield e2e, provisioner
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


async def _rows(engine: AsyncEngine, sql: str) -> list[tuple[object, ...]]:
    async with engine.connect() as conn:
        return [tuple(r) for r in await conn.execute(text(sql))]


def _dm(bot: E2EBot, sent: tuple[Sent, ...]) -> list[Sent]:
    return [s for s in sent if bot.discord.is_dm(s.channel_id)]


async def test_consent_in_a_dm_sends_only_name_email_and_language(
    accounts: tuple[E2EBot, FakeProvisioner],
) -> None:
    bot, provisioner = accounts
    leo = bot.person("Leo")
    await bot.dm(leo).say(INTRODUCTION)  # name, birth date, phone, address, wallet, email
    here = bot.channel("general", leo)

    asked = await here.slash("account create", locale="pt-BR")

    [note] = [s for s in asked.sent if not bot.discord.is_dm(s.channel_id)]
    assert note.ephemeral and "mensagem direta" in note.text
    [consent] = _dm(bot, asked.sent)
    assert "• Nome: Leonardo Araujo dos Santos" in consent.text
    assert f"• Email: {EMAIL}" in consent.text
    assert "• Idioma: português" in consent.text
    assert "**não** é apagada" in consent.text and "convite" in consent.text
    assert [label for label, _ in consent.buttons] == ["Confirmar", "Cancelar"]
    assert provisioner.requests == [], "nothing is sent before Confirm"

    ana = Conversation(bot, bot.person("Ana"), None)
    refused = await ana.press(consent, "Confirmar")
    assert provisioner.requests == [] and "Só quem pediu" in refused.text

    dm = bot.dm(leo)
    confirmed = await dm.press(consent, "Confirmar")

    assert provisioner.requests == [
        ProvisioningRequest(email=EMAIL, name="Leonardo Araujo dos Santos", locale="pt-BR")
    ]
    [reply] = [s for s in confirmed.sent if s.buttons]
    assert reply.text.startswith("Se este endereço ainda não tiver uma conta CyberdyneAuth")
    assert await _rows(bot.engine, "SELECT email_hmac FROM account_consent") == [
        (email_hmac(KEY.encode(), EMAIL),)
    ]

    linked = await dm.press(reply, "Vincular minha conta")
    [link] = [s for s in linked.sent if CONSOLE in s.text]
    url = next(word for word in link.text.split() if word.startswith(CONSOLE))
    assert urlsplit(url).path == "/link"
    [code] = parse_qs(urlsplit(url).query)["code"]
    stored = await _rows(bot.engine, "SELECT code_sha256 FROM account_link_code")
    assert stored == [(hashlib.sha256(code.encode()).digest(),)]

    again = await here.slash("account create", locale="pt-BR")
    assert "Pode pedir de novo" in again.text
    assert len(provisioner.requests) == 1, "a second request within a day sends nothing"
    assert bot.seal.refused == []


async def test_without_an_email_the_address_is_typed_and_not_saved(
    accounts: tuple[E2EBot, FakeProvisioner],
) -> None:
    bot, provisioner = accounts
    bia = bot.person("Bia")
    dm = bot.dm(bia)

    asked = await dm.slash("account create")

    [prompt] = asked.sent
    assert "I don't have one saved for you" in prompt.text
    [modal] = (await dm.press(prompt, "Enter email")).modals
    wrong = await dm.submit(modal, "not an address")
    assert "doesn't look like an email address" in wrong.text

    [modal] = (await dm.press(prompt, "Enter email")).modals
    shown = await dm.submit(modal, "bia@example.com")
    [consent] = shown.sent
    assert "• Email: bia@example.com" in consent.text
    assert "• Name: Bia" in consent.text and "• Language: English" in consent.text

    cancelled = await dm.press(consent, "Cancel")
    assert "Nothing was sent" in cancelled.text and provisioner.requests == []

    [modal] = (await dm.press(prompt, "Enter email")).modals
    [consent] = (await dm.submit(modal, "bia@example.com")).sent
    await dm.press(consent, "Confirm")

    assert provisioner.requests == [
        ProvisioningRequest(email="bia@example.com", name="Bia", locale="en")
    ]
    assert await bot.fact_rows(bia) == [], "a typed address is not saved as a fact"
