"""Speech to text over any OpenAI-compatible `/audio/transcriptions` endpoint.

Plain httpx rather than the OpenAI SDK, so the request goes through the
process's `Edges.http_transport` like every other outbound call: an end-to-end
test that fakes the network fakes this too, and a real connection from a test
is refused by the seal rather than silently made.

One attempt, no retries. A person is waiting on this in a DM, and a retry loop
would turn a failing endpoint into a minute of typing indicator before the same
"I couldn't understand the audio".
"""

from __future__ import annotations

import httpx

from chatmemory.app.voice import TranscriptionFailed

_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/webm": "webm",
}
"""The endpoint decides the format by the file name's extension, so the name
sent is derived from the verified type, never taken from the upload."""


class OpenAICompatibleTranscriber:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/audio/transcriptions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._transport = transport

    async def transcribe(self, audio: bytes, media_type: str, filename: str) -> str:
        """The transcript text. `filename` is ignored; see `_EXTENSIONS`."""
        name = f"audio.{_EXTENSIONS.get(media_type, 'ogg')}"
        async with httpx.AsyncClient(
            timeout=self._timeout,
            # A redirect would re-send the recording to a host nobody configured.
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            try:
                response = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    data={"model": self._model, "response_format": "json"},
                    files={"file": (name, audio, media_type)},
                )
            except httpx.HTTPError as exc:
                raise TranscriptionFailed(type(exc).__name__) from exc
        return _parse(response)


def _parse(response: httpx.Response) -> str:
    if response.status_code != 200:
        raise TranscriptionFailed(f"status {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise TranscriptionFailed("not JSON") from exc
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise TranscriptionFailed("no text in the reply")
    return text
