"""Voice notes posted in channels, recorded as pending media by the ingest half.

Each message goes through the production gateway handler, so "no row" is the
gate production applies -- a DM, a private thread and a channel out of scope
never become a stored message, and a media row cannot exist without one. Only
metadata is recorded: nothing is downloaded (FakeWeb sees no request) and no
model is called.

#general is indexed and #leadership is not, in this deployment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import discord
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.discord_wire import IS_VOICE_MESSAGE, attachment_payload
from tests.e2e.harness.process import GENERAL, NOW, e2e_settings, start
from tests.e2e.harness.web import DISCORD_CDN_HOSTS, NetworkSeal

CDN_URL = f"https://{DISCORD_CDN_HOSTS[0]}/attachments/1/2/voice-message.ogg?ex=6&is=7&hm=8"


@pytest_asyncio.fixture
async def media_bot(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """Channel media recorded from a week ago, with only #general indexed."""
    settings = e2e_settings(e2e_database_url).model_copy(
        update={
            "indexed_channel_ids": frozenset({GENERAL}),
            "media_enabled_at": NOW - timedelta(days=7),
        }
    )
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


async def media_rows(bot: E2EBot) -> list[dict[str, Any]]:
    async with bot.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT message_id, kind, declared_type, byte_size, duration_secs, "
                "source_url, status, attempts, text FROM message_media ORDER BY id"
            )
        )
        return [dict(r) for r in rows.mappings()]


async def test_a_voice_note_outside_an_indexed_channel_is_never_recorded(
    media_bot: E2EBot,
) -> None:
    bea = media_bot.person("Bea")
    wire, at = media_bot.discord, NOW - timedelta(minutes=5)
    hits, calls = len(media_bot.web.calls), len(media_bot.chat.calls)

    def voice_note(where: discord.TextChannel | discord.Thread) -> discord.Message:
        note = [attachment_payload(CDN_URL)]
        return wire.chatter(bea, where, "", at=at, attachments=note, flags=IS_VOICE_MESSAGE)

    in_dm = wire.dm_message(
        bea, "", attachments=[attachment_payload(CDN_URL)], flags=IS_VOICE_MESSAGE, at=at
    )
    for raw in (
        in_dm,
        voice_note(wire.private_thread("general")),
        voice_note(wire.channel("leadership")),
    ):
        assert await media_bot.ingest.deliver(raw) is None

    assert await media_rows(media_bot) == []
    # The same note in #general is recorded, so the gate above is the channel.
    indexed = voice_note(wire.channel("general"))
    assert await media_bot.ingest.deliver(indexed) is not None
    assert [r["message_id"] for r in await media_rows(media_bot)] == [indexed.id]
    assert len(media_bot.web.calls) == hits
    assert len(media_bot.chat.calls) == calls


async def test_a_voice_note_in_an_indexed_channel_is_one_pending_row(
    media_bot: E2EBot,
) -> None:
    bea = media_bot.person("Bea")
    raw = media_bot.discord.chatter(
        bea,
        media_bot.discord.channel("general"),
        "",
        at=NOW - timedelta(minutes=5),
        attachments=[attachment_payload(CDN_URL, size=48_000, duration=6.5)],
        flags=IS_VOICE_MESSAGE,
    )
    hits, calls = len(media_bot.web.calls), len(media_bot.chat.calls)

    stored = await media_bot.ingest.deliver(raw)
    # The gateway re-delivering it, as a resume or a reconciliation pass would,
    # refreshes the row rather than adding one.
    await media_bot.ingest.deliver(raw)

    assert stored is not None
    assert await media_rows(media_bot) == [
        {
            "message_id": raw.id,
            "kind": "voice",
            "declared_type": "audio/ogg",
            "byte_size": 48_000,
            "duration_secs": 6.5,
            "source_url": CDN_URL,
            "status": "pending",
            "attempts": 0,
            "text": None,
        }
    ]
    assert len(media_bot.web.calls) == hits
    assert len(media_bot.chat.calls) == calls
