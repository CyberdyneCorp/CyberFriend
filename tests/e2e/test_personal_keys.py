"""A CyberWealth connected-app key through the assembled process and real Postgres.

Sent in a DM it is stored sealed, answered by its last four characters and
nothing else, never reaches a model, and is listed by `/privacy` and removed
by `/forget`. Pasted into a channel it is neither archived nor answered, and
its author is warned by DM, as is the author of a channel message edited into
carrying one. Typed into `/suggest` or `/schedule create` it is never stored
as a suggestion or a scheduled question.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import discord
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
        update={
            "personal_secrets_key": SecretStr("A" * 43 + "="),
            "scheduled_tasks_enabled": True,
        }
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


async def _count(bot: E2EBot, table: str) -> int:
    async with bot.engine.connect() as conn:
        return int((await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


async def test_a_key_in_a_suggestion_is_not_stored_or_shown_to_admins(keyed: E2EBot) -> None:
    ana = keyed.person("Ana")

    turn = await keyed.channel("general", ana).slash("suggest", text=f"use my key {KEY}")

    assert not turn.searched and turn.schemas == ()
    assert turn.sent and all(s.ephemeral for s in turn.sent)
    assert KEY not in turn.text and SECRET[:-4] not in turn.text
    assert await _count(keyed, "feature_request") == 0
    assert await _ciphertexts(keyed) == []


async def test_a_key_in_a_scheduled_question_is_not_scheduled(keyed: E2EBot) -> None:
    ana = keyed.person("Ana")
    dm = keyed.dm(ana)
    await dm.say("oi")  # someone the bot has talked to, so a schedule could be kept

    turn = await dm.slash("schedule create", question=f"check {KEY} daily", every_hours=24)

    assert not turn.searched and turn.schemas == ()
    assert turn.sent and all(s.ephemeral for s in turn.sent)
    assert KEY not in turn.text and SECRET[:-4] not in turn.text
    assert await _count(keyed, "scheduled_task") == 0


def _edited(
    before: discord.Message | None, after: discord.Message
) -> discord.RawMessageUpdateEvent:
    data: Any = {"id": str(after.id)}
    event = discord.RawMessageUpdateEvent(data, after)
    event.cached_message = before
    return event


async def test_a_channel_message_edited_into_a_key_warns_its_author(keyed: E2EBot) -> None:
    ana = keyed.person("Ana")
    general = keyed.discord.channel("general")
    at = NOW - timedelta(minutes=1)
    before = keyed.discord.chatter(ana, general, "my key is below", at=at)
    after = keyed.discord.chatter(ana, general, f"my key is {KEY}", at=at)
    client = keyed.discord.client

    uncached = await keyed.turn(lambda: client.on_raw_message_edit(_edited(None, after)))
    newly = await keyed.turn(lambda: client.on_raw_message_edit(_edited(before, after)))
    again = await keyed.turn(lambda: client.on_raw_message_edit(_edited(after, after)))

    for warned in (uncached, newly):
        assert [s.via for s in warned.sent] == ["dm"]
        assert "#general" in warned.text and "revoke" in warned.text
        assert KEY not in warned.text and warned.schemas == ()
    assert again.sent == (), "already warned when it was sent with the key"
    assert await _ciphertexts(keyed) == []


async def test_an_edit_in_a_dm_or_without_a_key_is_left_alone(keyed: E2EBot) -> None:
    ana = keyed.person("Ana")
    general = keyed.discord.channel("general")
    plain = keyed.discord.chatter(ana, general, "no key here", at=NOW)
    in_dm = keyed.discord.dm_message(ana, f"my key {KEY}")
    client = keyed.discord.client

    for message in (plain, in_dm):
        event = _edited(None, message)
        turn = await keyed.turn(lambda: client.on_raw_message_edit(event))  # noqa: B023
        assert turn.sent == ()
    assert await _ciphertexts(keyed) == []
