"""Voice questions in a DM, through the assembled bot.

The CDN and the transcription endpoint are FakeWeb hosts scripted per
scenario, and neither is a default fixture: a scenario that reaches one it did
not script fails the turn. So "zero hosts reached" is asserted twice over --
by `turn.hosts`, and by the harness refusing any unscripted request.

The spoken question is the one test_said_by asks by typing, so what is checked
is that a voice note takes exactly the typed path: the said-by route, its
Portuguese header, the citation, and the transcript remembered as the question.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.formatting import split_message
from chatmemory.app.language import Language
from chatmemory.app.localise import PORTUGUESE
from chatmemory.app.reasoning.contract import NOTHING_FOUND
from chatmemory.app.voice import VOICE_REPLIES, VoiceRefusal
from chatmemory.domain.identity import PersonRef
from tests.audio import TOC_120MS, ogg_opus
from tests.e2e.harness.conversation import PLATFORM, E2EBot
from tests.e2e.harness.discord_wire import attachment_payload
from tests.e2e.harness.process import e2e_settings, start
from tests.e2e.harness.web import (
    DISCORD_CDN_HOSTS,
    MEDIA_HOSTS,
    NetworkSeal,
    ScriptedTranscription,
    serve_bytes,
)

CDN_HOST = DISCORD_CDN_HOSTS[0]
MEDIA_HOST = MEDIA_HOSTS[0]
CDN_URL = f"https://{CDN_HOST}/attachments/1/2/voice-message.ogg?ex=6&is=7&hm=8"
OGG = ogg_opus(3.0)

SPOKEN = "o que o Leo disse sobre o deploy semana passada?"
IN_WEEK = "the deploy is scheduled for friday night"
MARS = "o que aconteceu com o foguete da equipe de marte?"


@pytest_asyncio.fixture
async def voice_bot(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """The production process with voice questions on, and a 1-minute personal cap."""
    settings = e2e_settings(e2e_database_url).model_copy(
        update={
            "voice_questions_enabled": True,
            "media_api_key": SecretStr("e2e-media"),
            "voice_person_monthly_minutes": 1,
        }
    )
    e2e = await start(settings, clean, sealed_network)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


def speak(bot: E2EBot, words: str) -> ScriptedTranscription:
    """Serve the voice note from the CDN, and have it transcribed as `words`."""
    transcription = ScriptedTranscription(words)
    bot.web.script(CDN_HOST, serve_bytes(OGG))
    bot.web.script(MEDIA_HOST, transcription)
    return transcription


async def usage_seconds(bot: E2EBot) -> int:
    async with bot.engine.connect() as conn:
        total = await conn.scalar(text("SELECT COALESCE(SUM(seconds), 0) FROM media_usage"))
    return int(total)


async def test_a_portuguese_voice_question_is_answered_as_if_typed(voice_bot: E2EBot) -> None:
    bot = voice_bot
    leo, bea = bot.person("Leo"), bot.person("Bea")
    lastweek = datetime(2026, 8, 27, 10, tzinfo=timezone(timedelta(hours=-3)))
    await bot.seed_conversation(
        "general", [(PersonRef(PLATFORM, leo.id), "Leo", IN_WEEK, lastweek)]
    )
    transcription = speak(bot, SPOKEN)
    dm = bot.dm(bea)

    turn = await dm.say_voice(attachment_payload(CDN_URL, duration=4.2))

    assert turn.hosts == {CDN_HOST, MEDIA_HOST}
    first = turn.sent[0].content
    assert first.startswith(f'-# \U0001f3a4 "{SPOKEN}"\n'), first
    assert "-# O que Leo disse sobre o deploy" in turn.text
    assert IN_WEEK in turn.text
    [request] = transcription.requests
    assert OGG in request.content and b"gpt-4o-mini-transcribe" in request.content
    assert request.headers["authorization"] == "Bearer e2e-media"
    # The transcript is remembered as the question; the audio is nowhere.
    [remembered] = await dm.memory_turns()
    assert remembered.question == SPOKEN
    assert await usage_seconds(bot) == 5


async def test_switched_off_a_voice_note_gets_one_reply_and_reaches_no_host(bot: E2EBot) -> None:
    ana = bot.person("Ana")
    dm = bot.dm(ana)
    await dm.say("fale comigo em português")

    turn = await dm.say_voice(attachment_payload(CDN_URL))

    assert turn.hosts == frozenset()
    assert not turn.searched and not turn.schemas
    assert turn.text == VOICE_REPLIES[VoiceRefusal.DISABLED][Language.PORTUGUESE]
    turn.assert_language("pt")

    english = await bot.dm(bot.person("Bea")).say_voice(attachment_payload(CDN_URL))
    assert english.text == VOICE_REPLIES[VoiceRefusal.DISABLED][Language.ENGLISH]
    assert english.hosts == frozenset()


async def test_over_the_personal_cap_nothing_is_downloaded_or_sent(voice_bot: E2EBot) -> None:
    bot = voice_bot
    transcription = speak(bot, MARS)
    dm = bot.dm(bot.person("Ana"))

    answered = await dm.say_voice(attachment_payload(CDN_URL, duration=40.0))
    assert PORTUGUESE[NOTHING_FOUND] in answered.text
    answered.assert_language("pt")

    refused = await dm.say_voice(attachment_payload(CDN_URL, duration=30.0))

    assert refused.hosts == frozenset()
    assert len(transcription.requests) == 1
    assert refused.text == VOICE_REPLIES[VoiceRefusal.PERSON_CAP][Language.ENGLISH]
    assert await usage_seconds(bot) == 40


async def test_a_url_off_the_discord_cdn_is_never_fetched(voice_bot: E2EBot) -> None:
    bot = voice_bot
    speak(bot, SPOKEN)
    bot.web.script("evil.example", serve_bytes(OGG))

    turn = await bot.dm(bot.person("Ana")).say_voice(
        attachment_payload("https://evil.example/attachments/voice-message.ogg")
    )

    assert turn.hosts == frozenset()
    assert turn.text == VOICE_REPLIES[VoiceRefusal.UNSUPPORTED][Language.ENGLISH]
    assert await usage_seconds(bot) == 0


async def test_a_cdn_redirect_is_not_followed(voice_bot: E2EBot) -> None:
    bot = voice_bot
    transcription = speak(bot, SPOKEN)
    bot.web.script(
        CDN_HOST,
        lambda _: httpx.Response(302, headers={"location": "https://evil.example/voice.ogg"}),
    )
    bot.web.script("evil.example", serve_bytes(OGG))

    turn = await bot.dm(bot.person("Ana")).say_voice(attachment_payload(CDN_URL, duration=4.2))

    assert turn.hosts == {CDN_HOST}
    assert transcription.requests == []
    assert turn.text == VOICE_REPLIES[VoiceRefusal.UNHEARD][Language.ENGLISH]


async def test_audio_longer_than_it_declared_is_never_sent(voice_bot: E2EBot) -> None:
    bot = voice_bot
    transcription = speak(bot, SPOKEN)
    # A modified client declares one second for forty minutes of Opus.
    bot.web.script(CDN_HOST, serve_bytes(ogg_opus(2400, packet=TOC_120MS)))

    turn = await bot.dm(bot.person("Ana")).say_voice(attachment_payload(CDN_URL, duration=1.0))

    assert turn.hosts == {CDN_HOST}
    assert transcription.requests == []
    assert turn.text == VOICE_REPLIES[VoiceRefusal.TOO_LONG][Language.ENGLISH].format(seconds=120)
    assert await usage_seconds(bot) == 1


async def test_a_voice_note_in_a_channel_is_not_heard(voice_bot: E2EBot) -> None:
    bot = voice_bot
    speak(bot, SPOKEN)

    turn = await bot.channel("general", bot.person("Ana")).say_voice(attachment_payload(CDN_URL))

    assert turn.hosts == frozenset()
    described = bot.process.stack.capabilities.describe(Language.ENGLISH)
    assert turn.text == "\n".join(split_message(described))
    assert "voice message" in turn.text
    assert await usage_seconds(bot) == 0


async def test_a_rate_limited_asker_is_refused_before_anything_is_fetched(
    voice_bot: E2EBot,
) -> None:
    bot = voice_bot
    transcription = speak(bot, SPOKEN)
    ana = bot.person("Ana")
    # Spend Ana's hourly allowance as twenty typed questions would.
    limiter = bot.process.graph.asks._limiter
    while limiter.check(PersonRef(PLATFORM, ana.id)).allowed:
        pass

    turn = await bot.dm(ana).say_voice(attachment_payload(CDN_URL))

    assert turn.hosts == frozenset()
    assert transcription.requests == []
    assert "You've hit your question limit" in turn.text
    assert await usage_seconds(bot) == 0
