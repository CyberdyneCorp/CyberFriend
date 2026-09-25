"""Voice questions: a voice message sent to the bot in a DM, heard as typed text.

The recording is somebody's voice, which is personal data in a way their typed
words are not, and it leaves the process for a transcription endpoint. So the
order below is the design, and each step can only refuse:

1. The attachment is checked from its metadata alone: exactly one, an
   allowlisted audio type, a Discord CDN URL, under the byte and duration
   limits. Nothing is fetched to find out it was too long.
2. The person and the month are checked -- opted out, over their own monthly
   minutes, or over the deployment's -- and the minutes are charged in the
   same transaction, by the duration Discord declares. So the caps bound what
   is *sent*, not what was spent afterwards; a download that then fails is not
   refunded, because the cap is the ceiling on the bill, not an estimate of it.
3. Only then is the audio downloaded, bounded, and its magic bytes checked
   against the type it claimed. A disagreement is refused, as for documents.
4. The transcript is the question. The audio is discarded and never stored;
   the transcript goes where typed text goes, and nowhere else.

Every failure from step 3 on is one reply -- "I couldn't understand the audio"
-- and never an exception: a person who sent a voice note is owed an answer
in words, not silence.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

import structlog

from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.documents.ports import ContentFetcher
from chatmemory.app.language import Language
from chatmemory.domain.identity import PersonRef

log = structlog.get_logger()

AUDIO_TYPES = frozenset({"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/webm"})
"""Declared types a voice question may have. A Discord voice message is Opus in
Ogg; the rest are what phones and desktop recorders save."""

DISCORD_CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
"""The only hosts audio is fetched from.

A closed set rather than "whatever URL the attachment names": the URL is in a
payload the bot did not write, and fetching it would let a message point the
process at any host it can reach."""

HEARD_CHARS = 200
"""How much of the transcript is quoted back above the answer."""


class VoiceRefusal(StrEnum):
    """Why a voice question was not heard. Each has one fixed reply."""

    DISABLED = "disabled"
    ONE_AT_A_TIME = "one_at_a_time"
    UNSUPPORTED = "unsupported"
    TOO_LONG = "too_long"
    OPTED_OUT = "opted_out"
    PERSON_CAP = "person_cap"
    MONTHLY_CAP = "monthly_cap"
    UNHEARD = "unheard"


class Reservation(StrEnum):
    """What the usage ledger said to charging a voice question's minutes."""

    GRANTED = "granted"
    OPTED_OUT = "opted_out"
    PERSON_CAP = "person_cap"
    MONTHLY_CAP = "monthly_cap"


_REFUSED_RESERVATION = {
    Reservation.OPTED_OUT: VoiceRefusal.OPTED_OUT,
    Reservation.PERSON_CAP: VoiceRefusal.PERSON_CAP,
    Reservation.MONTHLY_CAP: VoiceRefusal.MONTHLY_CAP,
}


@dataclass(frozen=True, slots=True)
class VoiceClip:
    """One attachment, as the platform described it. Nothing here is verified."""

    url: str
    content_type: str
    size: int
    duration_seconds: float | None = None
    filename: str = ""


@dataclass(frozen=True, slots=True)
class VoiceLimits:
    max_seconds: int
    max_bytes: int
    person_monthly_seconds: int
    overall_monthly_seconds: int


@dataclass(frozen=True, slots=True)
class Heard:
    """A transcript, or the reason there is none."""

    transcript: str = ""
    refusal: VoiceRefusal | None = None


class TranscriptionFailed(Exception):
    """The endpoint did not return a transcript."""


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, media_type: str, filename: str) -> str:
        """The words spoken, or raise `TranscriptionFailed`."""
        ...


class VoiceLedger(Protocol):
    async def reserve(
        self, person: PersonRef, seconds: int, month: date, limits: VoiceLimits
    ) -> Reservation:
        """Charge `seconds` to `person` for `month` if the opt-out and both caps allow.

        One transaction: the check and the charge cannot be separated, so two
        voice notes arriving together cannot both fit under the last minute.
        """
        ...


def declared_type(content_type: str | None) -> str:
    """The media type without parameters, lowercased: `audio/ogg; codecs=opus` -> `audio/ogg`."""
    return (content_type or "").split(";")[0].strip().lower()


def is_audio(content_type: str | None) -> bool:
    return declared_type(content_type).startswith("audio/")


def from_discord_cdn(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "") in DISCORD_CDN_HOSTS


def check_clips(clips: Sequence[VoiceClip], limits: VoiceLimits) -> VoiceRefusal | None:
    """Refuse from metadata alone, before anything is charged or fetched."""
    if len(clips) != 1:
        return VoiceRefusal.ONE_AT_A_TIME
    clip = clips[0]
    if clip.content_type not in AUDIO_TYPES or not from_discord_cdn(clip.url):
        return VoiceRefusal.UNSUPPORTED
    if clip.size > limits.max_bytes:
        return VoiceRefusal.TOO_LONG
    if clip.duration_seconds is not None and clip.duration_seconds > limits.max_seconds:
        return VoiceRefusal.TOO_LONG
    return None


def charged_seconds(clip: VoiceClip, limits: VoiceLimits) -> int:
    """What a clip costs against the caps: its declared duration, rounded up.

    A clip that declares none is charged the whole limit, so an unknown length
    can never slip under a cap it would not fit."""
    if clip.duration_seconds is None:
        return limits.max_seconds
    return max(1, math.ceil(clip.duration_seconds))


def month_of(moment_date: date) -> date:
    """The calendar month a charge belongs to, as its first day."""
    return moment_date.replace(day=1)


def sniffed_type(data: bytes) -> str | None:
    """The audio type the bytes are, by their magic, or None."""
    head = data[:16]
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if head[4:8] == b"ftyp":
        return "audio/mp4"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    return None


def heard_excerpt(transcript: str) -> str:
    """The transcript on one line, cut at a word boundary with an ellipsis."""
    flat = " ".join(transcript.split())
    if len(flat) <= HEARD_CHARS:
        return flat
    cut = flat[: HEARD_CHARS - 1]
    spaced = cut.rsplit(" ", 1)[0]
    return (spaced if len(spaced) >= HEARD_CHARS // 2 else cut).rstrip(" ,.;:") + "…"


class VoiceQuestions:
    """Turns one voice attachment into the text of a question, or a refusal."""

    def __init__(
        self,
        transcriber: Transcriber,
        fetcher: ContentFetcher,
        ledger: VoiceLedger,
        limits: VoiceLimits,
        *,
        fetch_timeout: float,
        clock: Clock = utc_now,
    ) -> None:
        self._transcriber = transcriber
        self._fetcher = fetcher
        self._ledger = ledger
        self._limits = limits
        self._fetch_timeout = fetch_timeout
        self._clock = clock

    @property
    def limits(self) -> VoiceLimits:
        return self._limits

    async def hear(self, person: PersonRef, clips: Sequence[VoiceClip]) -> Heard:
        refusal = check_clips(clips, self._limits)
        if refusal is not None:
            log.info("voice.refused", reason=str(refusal))
            return Heard(refusal=refusal)
        clip = clips[0]
        seconds = charged_seconds(clip, self._limits)
        try:
            reservation = await self._ledger.reserve(
                person, seconds, month_of(self._clock().date()), self._limits
            )
        except Exception:
            # Fail closed: without a ledger there is no cap, and nothing is sent.
            log.exception("voice.ledger_failed")
            return Heard(refusal=VoiceRefusal.UNHEARD)
        if reservation is not Reservation.GRANTED:
            log.info("voice.refused", reason=str(reservation))
            return Heard(refusal=_REFUSED_RESERVATION[reservation])
        return await self._transcribe(clip, seconds)

    async def _transcribe(self, clip: VoiceClip, seconds: int) -> Heard:
        audio = await self._fetcher.fetch(clip.url, self._limits.max_bytes, self._fetch_timeout)
        if audio is None or sniffed_type(audio) != clip.content_type:
            log.info("voice.unreadable", fetched=audio is not None, claimed=clip.content_type)
            return Heard(refusal=VoiceRefusal.UNHEARD)
        try:
            text = await self._transcriber.transcribe(audio, clip.content_type, clip.filename)
        except TranscriptionFailed as exc:
            log.warning("voice.transcription_failed", error=str(exc))
            return Heard(refusal=VoiceRefusal.UNHEARD)
        transcript = " ".join(text.split())
        # The length only, never the words: the transcript is the question.
        log.info("voice.heard", seconds=seconds, chars=len(transcript))
        if not transcript:
            return Heard(refusal=VoiceRefusal.UNHEARD)
        return Heard(transcript=transcript)


VOICE_REPLIES: dict[VoiceRefusal, dict[Language, str]] = {
    VoiceRefusal.DISABLED: {
        Language.ENGLISH: (
            "Voice questions aren't enabled here, so I didn't listen to that. "
            "Please type your question."
        ),
        Language.PORTUGUESE: (
            "Perguntas por áudio não estão habilitadas aqui, então não ouvi esse "
            "áudio. Por favor, digite sua pergunta."
        ),
    },
    VoiceRefusal.ONE_AT_A_TIME: {
        Language.ENGLISH: "Please send me one voice message at a time, with nothing else attached.",
        Language.PORTUGUESE: "Por favor, me mande um áudio por vez, sem outros anexos.",
    },
    VoiceRefusal.UNSUPPORTED: {
        Language.ENGLISH: (
            "I can't listen to that kind of file. Send me a Discord voice message, "
            "or type your question."
        ),
        Language.PORTUGUESE: (
            "Não consigo ouvir esse tipo de arquivo. Me mande uma mensagem de voz "
            "do Discord, ou digite sua pergunta."
        ),
    },
    VoiceRefusal.TOO_LONG: {
        Language.ENGLISH: (
            "That audio is too long for me: a voice question can be up to "
            "{seconds} seconds. Send a shorter one, or type your question."
        ),
        Language.PORTUGUESE: (
            "Esse áudio é longo demais para mim: uma pergunta por áudio pode ter "
            "até {seconds} segundos. Mande um mais curto, ou digite sua pergunta."
        ),
    },
    VoiceRefusal.OPTED_OUT: {
        Language.ENGLISH: (
            "You've opted out of having your data processed, so I don't "
            "transcribe your voice. Please type your question."
        ),
        Language.PORTUGUESE: (
            "Você optou por não ter seus dados processados, então não transcrevo "
            "sua voz. Por favor, digite sua pergunta."
        ),
    },
    VoiceRefusal.PERSON_CAP: {
        Language.ENGLISH: (
            "You've used all of your voice questions for this month. Please type "
            "your question; voice comes back next month."
        ),
        Language.PORTUGUESE: (
            "Você já usou todas as suas perguntas por áudio deste mês. Por favor, "
            "digite sua pergunta; o áudio volta no mês que vem."
        ),
    },
    VoiceRefusal.MONTHLY_CAP: {
        Language.ENGLISH: (
            "Voice questions have reached this server's limit for the month. "
            "Please type your question."
        ),
        Language.PORTUGUESE: (
            "As perguntas por áudio atingiram o limite deste servidor no mês. Por "
            "favor, digite sua pergunta."
        ),
    },
    VoiceRefusal.UNHEARD: {
        Language.ENGLISH: (
            "I couldn't understand the audio. Please try again, or type your question."
        ),
        Language.PORTUGUESE: (
            "Não consegui entender o áudio. Por favor, tente de novo ou digite sua pergunta."
        ),
    },
}
"""One fixed reply per refusal, per language. No wording says more than the
refusal itself: the per-person and server caps are told apart because the
person can act on the difference, and nothing else is."""


def voice_reply(refusal: VoiceRefusal, language: Language, *, max_seconds: int = 0) -> str:
    """The fixed reply, in Portuguese for a Portuguese speaker, else English."""
    key = Language.PORTUGUESE if language is Language.PORTUGUESE else Language.ENGLISH
    return VOICE_REPLIES[refusal][key].format(seconds=max_seconds)
