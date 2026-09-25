"""The OpenAI-compatible transcriber: the request it sends, and what it accepts back.

Over an `httpx.MockTransport`, which is how the process's `Edges.http_transport`
reaches it, so this is the request production sends.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from chatmemory.adapters.llm.transcription import OpenAICompatibleTranscriber
from chatmemory.app.voice import TranscriptionFailed
from chatmemory.config import Settings

OGG = b"OggS" + b"\x00" * 32

BASE = {
    "discord_token": "t",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
}


def transcriber(handler: httpx.MockTransport) -> OpenAICompatibleTranscriber:
    return OpenAICompatibleTranscriber(
        base_url="https://api.openai.com/v1/",
        api_key="media-key",
        model="gpt-4o-mini-transcribe",
        timeout_seconds=5.0,
        transport=handler,
    )


async def test_the_request_is_a_multipart_post_to_audio_transcriptions() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "o que decidimos?"})

    text = await transcriber(httpx.MockTransport(handle)).transcribe(
        OGG, "audio/ogg", "../../etc/passwd"
    )

    assert text == "o que decidimos?"
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.com/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer media-key"
    body = request.content
    assert b'name="model"\r\n\r\ngpt-4o-mini-transcribe' in body
    assert b'name="response_format"\r\n\r\njson' in body
    # The name comes from the verified type, never from the upload.
    assert b'filename="audio.ogg"' in body and b"passwd" not in body
    assert OGG in body


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(401, json={"error": "bad key"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"nope": 1}),
        httpx.Response(200, json=["text"]),
        httpx.Response(302, headers={"location": "https://evil.example/"}),
    ],
)
async def test_anything_but_a_transcript_is_a_failure(response: httpx.Response) -> None:
    adapter = transcriber(httpx.MockTransport(lambda request: response))

    with pytest.raises(TranscriptionFailed):
        await adapter.transcribe(OGG, "audio/ogg", "")


async def test_a_redirect_is_not_followed() -> None:
    hosts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(307, headers={"location": "https://evil.example/steal"})

    with pytest.raises(TranscriptionFailed):
        await transcriber(httpx.MockTransport(handle)).transcribe(OGG, "audio/ogg", "")
    assert hosts == ["api.openai.com"]


async def test_a_transport_error_is_a_failure() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(TranscriptionFailed):
        await transcriber(httpx.MockTransport(handle)).transcribe(OGG, "audio/ogg", "")


# --- settings -------------------------------------------------------------


def test_voice_questions_are_off_by_default_with_the_documented_limits() -> None:
    settings = Settings(**BASE)
    assert settings.voice_questions_enabled is False
    assert settings.voice_max_seconds == 120
    assert settings.voice_person_monthly_minutes == 60
    assert settings.media_audio_monthly_minutes == 1500
    assert settings.media_audio_model == "gpt-4o-mini-transcribe"
    assert settings.media_base_url == "https://api.openai.com/v1"


def test_enabling_voice_without_a_key_is_a_boot_failure() -> None:
    with pytest.raises(ValidationError, match="media_api_key"):
        Settings(**BASE, voice_questions_enabled=True)
    assert Settings(**BASE, voice_questions_enabled=True, media_api_key="m").media_api_key


@pytest.mark.parametrize(
    "field", ["voice_max_seconds", "voice_person_monthly_minutes", "media_audio_monthly_minutes"]
)
def test_a_zero_voice_limit_is_a_boot_failure(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**BASE, **{field: 0})


def test_the_media_endpoint_must_be_http() -> None:
    with pytest.raises(ValidationError, match="media_base_url"):
        Settings(**BASE, media_base_url="ftp://x")
