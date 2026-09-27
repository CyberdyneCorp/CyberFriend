"""A CyberWealth connected-app key through the assembled process and real Postgres.

Sent in a DM it is stored sealed, answered by its last four characters and
nothing else, never reaches a model, and is listed by `/privacy` and removed
by `/forget`. Pasted into a channel it is neither archived nor answered, and
its author is warned by DM.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.process import NOW, e2e_settings, start
from tests.e2e.harness.web import NetworkSeal

SECRET = "a1B2-c3D4_" * 4 + "Zq9"
KEY = f"cwk_live_ABCDEFGHJK_{SECRET}"


@pytest_asyncio.fixture
async def keyed(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    settings = e2e_settings(e2e_database_url).model_copy(
        update={"personal_secrets_key": SecretStr("A" * 43 + "=")}
    )
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


async def _ciphertexts(bot: E2EBot) -> list[bytes]:
    async with bot.engine.connect() as conn:
        rows = await conn.execute(text("SELECT ciphertext FROM person_secret"))
        return [bytes(r[0]) for r in rows]


async def test_a_key_sent_in_a_dm_is_sealed_listed_and_forgotten(keyed: E2EBot) -> None:
    leo = keyed.person("Leo")
    dm = keyed.dm(leo)

    sent = await dm.say(f"minha chave do cyberwealth é {KEY}")

    assert not sent.searched and sent.schemas == ()
    assert KEY[-4:] in sent.text and SECRET[:-4] not in sent.text
    assert "chave do CyberWealth" in sent.text
    [sealed] = await _ciphertexts(keyed)
    assert KEY.encode() not in sealed
    assert await dm.memory_turns() == []

    shown = await dm.slash("privacy")
    assert f"key ending `{KEY[-4:]}`" in shown.text and SECRET[:-4] not in shown.text

    forgot = await dm.slash("forget", scope="everywhere")
    assert "CyberWealth key was deleted" in forgot.text
    assert await _ciphertexts(keyed) == []


async def test_a_key_in_a_channel_is_not_archived_or_answered_and_its_author_is_warned(
    keyed: E2EBot,
) -> None:
    ana = keyed.person("Ana")
    general = keyed.discord.channel("general")

    stored = await keyed.ingest.deliver(
        keyed.discord.chatter(ana, general, f"my key {KEY}", at=NOW - timedelta(minutes=1))
    )
    assert stored is None

    warned = await keyed.channel("general", ana).say(f"is this right? {KEY}")

    assert not warned.searched and warned.schemas == ()
    assert [s.via for s in warned.sent] == ["dm"]
    assert "#general" in warned.text and "revoke" in warned.text
    assert KEY not in warned.text
    assert await _ciphertexts(keyed) == []
