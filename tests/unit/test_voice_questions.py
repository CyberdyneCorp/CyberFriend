"""Voice questions: what is refused before anything is charged, fetched or sent.

The order is the privacy argument, so most of these check that a later step
never ran: a clip refused by its metadata never reaches the ledger, a clip the
ledger refuses is never downloaded, and bytes that are not the audio they
claimed are never sent to the transcriber.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from chatmemory.adapters.discord.bot import heard_line
from chatmemory.app.language import Language, detect
from chatmemory.app.self_description import describe_capabilities
from chatmemory.app.voice import (
    AUDIO_TYPES,
    HEARD_CHARS,
    VOICE_REPLIES,
    Reservation,
    TranscriptionFailed,
    VoiceClip,
    VoiceLimits,
    VoiceQuestions,
    VoiceRefusal,
    charged_seconds,
    check_clips,
    declared_type,
    from_discord_cdn,
    heard_excerpt,
    month_of,
    sniffed_type,
    voice_reply,
)
from chatmemory.domain.identity import PersonRef

ANA = PersonRef("discord", 4242)
CDN = "https://cdn.discordapp.com/attachments/1/2/voice-message.ogg?ex=1&is=2&hm=3"
OGG = b"OggS\x00\x02" + b"\x00" * 64
LIMITS = VoiceLimits(
    max_seconds=120,
    max_bytes=10_000_000,
    person_monthly_seconds=3600,
    overall_monthly_seconds=90_000,
)
NOW = datetime(2026, 9, 17, 23, 59, tzinfo=UTC)


def clip(**changes: object) -> VoiceClip:
    base = VoiceClip(url=CDN, content_type="audio/ogg", size=40_000, duration_seconds=7.2)
    return replace(base, **changes)  # type: ignore[arg-type]


# --- metadata checks ------------------------------------------------------


def test_a_discord_voice_note_passes_the_metadata_checks() -> None:
    assert check_clips([clip()], LIMITS) is None


@pytest.mark.parametrize("count", [0, 2])
def test_exactly_one_attachment_is_heard(count: int) -> None:
    assert check_clips([clip()] * count, LIMITS) is VoiceRefusal.ONE_AT_A_TIME


@pytest.mark.parametrize("media", sorted(AUDIO_TYPES))
def test_every_allowlisted_type_is_accepted(media: str) -> None:
    assert check_clips([clip(content_type=media)], LIMITS) is None


@pytest.mark.parametrize("media", ["audio/flac", "video/mp4", "image/png", "", "audio/x-wav"])
def test_a_type_off_the_allowlist_is_refused(media: str) -> None:
    assert check_clips([clip(content_type=media)], LIMITS) is VoiceRefusal.UNSUPPORTED


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/voice.ogg",
        "http://cdn.discordapp.com/attachments/1/2/v.ogg",
        "https://cdn.discordapp.com.evil.example/v.ogg",
        "https://user@evil.example/cdn.discordapp.com/v.ogg",
        "file:///etc/passwd",
    ],
)
def test_only_the_discord_cdn_over_https_is_accepted(url: str) -> None:
    assert not from_discord_cdn(url)
    assert check_clips([clip(url=url)], LIMITS) is VoiceRefusal.UNSUPPORTED


def test_both_cdn_hosts_are_accepted() -> None:
    assert from_discord_cdn(CDN)
    assert from_discord_cdn("https://media.discordapp.net/attachments/1/2/a.mp3")


def test_over_the_duration_or_byte_limit_is_too_long() -> None:
    assert check_clips([clip(duration_seconds=120.5)], LIMITS) is VoiceRefusal.TOO_LONG
    assert check_clips([clip(size=10_000_001)], LIMITS) is VoiceRefusal.TOO_LONG
    assert check_clips([clip(duration_seconds=120.0)], LIMITS) is None


def test_the_declared_type_drops_parameters_and_case() -> None:
    assert declared_type("Audio/OGG; codecs=opus") == "audio/ogg"
    assert declared_type(None) == ""


# --- cap arithmetic ------------------------------------------------------


def test_a_clip_is_charged_its_duration_rounded_up() -> None:
    assert charged_seconds(clip(duration_seconds=7.2), LIMITS) == 8
    assert charged_seconds(clip(duration_seconds=0.1), LIMITS) == 1
    assert charged_seconds(clip(duration_seconds=60.0), LIMITS) == 60


def test_a_clip_with_no_duration_is_charged_the_whole_limit() -> None:
    assert charged_seconds(clip(duration_seconds=None), LIMITS) == LIMITS.max_seconds


def test_a_charge_belongs_to_its_calendar_month() -> None:
    assert month_of(date(2026, 9, 30)) == date(2026, 9, 1)
    assert month_of(date(2026, 10, 1)) == date(2026, 10, 1)


# --- sniffing --------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (OGG, "audio/ogg"),
        (b"ID3\x04\x00" + b"\x00" * 20, "audio/mpeg"),
        (b"\xff\xfb\x90\x00" + b"\x00" * 20, "audio/mpeg"),
        (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 20, "audio/mp4"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20, "audio/wav"),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 20, "audio/webm"),
        (b"%PDF-1.7", None),
        (b"<html>", None),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", None),
        (b"", None),
    ],
)
def test_audio_is_recognised_by_its_magic_bytes(data: bytes, expected: str | None) -> None:
    assert sniffed_type(data) == expected


# --- the service --------------------------------------------------------------


class Ledger:
    def __init__(self, answer: Reservation = Reservation.GRANTED) -> None:
        self.answer = answer
        self.calls: list[tuple[PersonRef, int, date]] = []

    async def reserve(
        self, person: PersonRef, seconds: int, month: date, limits: VoiceLimits
    ) -> Reservation:
        self.calls.append((person, seconds, month))
        return self.answer


class Fetcher:
    def __init__(self, data: bytes | None = OGG) -> None:
        self.data = data
        self.urls: list[str] = []

    async def fetch(self, url: str, max_bytes: int, timeout: float) -> bytes | None:
        self.urls.append(url)
        return self.data


class Transcriber:
    def __init__(self, text: str = "o que decidimos sobre o deploy?") -> None:
        self.text = text
        self.sent: list[tuple[bytes, str]] = []

    async def transcribe(self, audio: bytes, media_type: str, filename: str) -> str:
        self.sent.append((audio, media_type))
        if self.text == "!fail":
            raise TranscriptionFailed("status 500")
        return self.text


def service(
    ledger: Ledger | None = None,
    fetcher: Fetcher | None = None,
    transcriber: Transcriber | None = None,
) -> tuple[VoiceQuestions, Ledger, Fetcher, Transcriber]:
    ledger = ledger or Ledger()
    fetcher = fetcher or Fetcher()
    transcriber = transcriber or Transcriber()
    voice = VoiceQuestions(
        transcriber, fetcher, ledger, LIMITS, fetch_timeout=5.0, clock=lambda: NOW
    )
    return voice, ledger, fetcher, transcriber


async def test_a_voice_note_is_charged_fetched_and_transcribed() -> None:
    voice, ledger, fetcher, transcriber = service()

    heard = await voice.hear(ANA, [clip()])

    assert heard.refusal is None
    assert heard.transcript == "o que decidimos sobre o deploy?"
    assert ledger.calls == [(ANA, 8, date(2026, 9, 1))]
    assert fetcher.urls == [CDN]
    assert transcriber.sent == [(OGG, "audio/ogg")]


async def test_a_metadata_refusal_charges_and_fetches_nothing() -> None:
    voice, ledger, fetcher, transcriber = service()

    heard = await voice.hear(ANA, [clip(url="https://evil.example/a.ogg")])

    assert heard.refusal is VoiceRefusal.UNSUPPORTED
    assert (ledger.calls, fetcher.urls, transcriber.sent) == ([], [], [])


@pytest.mark.parametrize(
    ("answer", "refusal"),
    [
        (Reservation.OPTED_OUT, VoiceRefusal.OPTED_OUT),
        (Reservation.PERSON_CAP, VoiceRefusal.PERSON_CAP),
        (Reservation.MONTHLY_CAP, VoiceRefusal.MONTHLY_CAP),
    ],
)
async def test_a_ledger_refusal_downloads_nothing(
    answer: Reservation, refusal: VoiceRefusal
) -> None:
    voice, _, fetcher, transcriber = service(ledger=Ledger(answer))

    heard = await voice.hear(ANA, [clip()])

    assert heard.refusal is refusal
    assert (fetcher.urls, transcriber.sent) == ([], [])


async def test_a_failing_ledger_fails_closed() -> None:
    class Broken(Ledger):
        async def reserve(self, *args: object, **kwargs: object) -> Reservation:
            raise RuntimeError("database down")

    voice, _, fetcher, _ = service(ledger=Broken())

    assert (await voice.hear(ANA, [clip()])).refusal is VoiceRefusal.UNHEARD
    assert fetcher.urls == []


async def test_bytes_that_are_not_the_claimed_audio_are_never_sent() -> None:
    voice, _, _, transcriber = service(fetcher=Fetcher(b"%PDF-1.7 not audio"))

    assert (await voice.hear(ANA, [clip()])).refusal is VoiceRefusal.UNHEARD
    assert transcriber.sent == []


@pytest.mark.parametrize(
    ("fetched", "said"), [(None, "x"), (OGG, "!fail"), (OGG, "   \n "), (OGG, "")]
)
async def test_every_later_failure_is_one_reply_and_never_an_exception(
    fetched: bytes | None, said: str
) -> None:
    voice, _, _, _ = service(fetcher=Fetcher(fetched), transcriber=Transcriber(said))

    assert (await voice.hear(ANA, [clip()])).refusal is VoiceRefusal.UNHEARD


async def test_the_transcript_is_flattened_to_one_line() -> None:
    voice, _, _, _ = service(transcriber=Transcriber("  what did\nwe   decide?  "))

    assert (await voice.hear(ANA, [clip()])).transcript == "what did we decide?"


# --- replies --------------------------------------------------------------


@pytest.mark.parametrize("refusal", list(VoiceRefusal))
def test_every_refusal_has_a_reply_in_each_language(refusal: VoiceRefusal) -> None:
    english = voice_reply(refusal, Language.ENGLISH, max_seconds=120)
    portuguese = voice_reply(refusal, Language.PORTUGUESE, max_seconds=120)
    assert detect(english) is Language.ENGLISH
    assert detect(portuguese) is Language.PORTUGUESE
    assert "{" not in english + portuguese


def test_an_unknown_language_is_answered_in_english() -> None:
    assert voice_reply(VoiceRefusal.UNHEARD, Language.UNKNOWN) == (
        VOICE_REPLIES[VoiceRefusal.UNHEARD][Language.ENGLISH]
    )


def test_the_length_limit_is_named_in_the_reply() -> None:
    assert "90" in voice_reply(VoiceRefusal.TOO_LONG, Language.PORTUGUESE, max_seconds=90)


def test_the_heard_line_is_small_quoted_escaped_and_clipped() -> None:
    line = heard_line("@everyone **olha** " + "palavra " * 60)

    assert line.startswith('-# \U0001f3a4 "') and line.endswith('…"')
    assert "\n" not in line
    assert "\\*\\*olha\\*\\*" in line
    assert "@everyone" not in line
    assert len(heard_excerpt("palavra " * 60)) <= HEARD_CHARS


# --- self-description ------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "phrase"),
    [(Language.ENGLISH, "voice message"), (Language.PORTUGUESE, "áudio")],
)
def test_the_description_offers_voice_only_where_it_is_on(language: Language, phrase: str) -> None:
    on = describe_capabilities((), language=language, voice_questions=True)
    off = describe_capabilities((), language=language)

    assert phrase in on
    assert phrase not in off
