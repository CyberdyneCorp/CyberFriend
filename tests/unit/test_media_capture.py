"""Which attachments capture records as pending media, and from when.

`media_of` reads plain objects shaped like discord.py's; the e2e suite drives
the same conversion with real `discord.Message`s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from chatmemory.adapters.discord.source import RawMessage, media_of, to_message
from chatmemory.config import Settings
from chatmemory.domain.media import MediaKind, MediaRef

T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
CDN = "https://cdn.discordapp.com/attachments/1/2/{name}?ex=1&is=2&hm=3"


@dataclass
class Author:
    id: int = 7
    bot: bool = False


@dataclass
class Chan:
    id: int = 100
    parent_id: int | None = None


@dataclass
class Flags:
    voice: bool = False


@dataclass
class Attachment:
    id: int
    content_type: str | None
    filename: str = "file"
    size: int = 1000
    url: str = ""
    duration: float | None = None

    def __post_init__(self) -> None:
        self.url = self.url or CDN.format(name=self.filename)


@dataclass
class Raw:
    attachments: list[Attachment] = field(default_factory=list)
    flags: Flags = field(default_factory=Flags)
    id: int = 1
    content: str = ""
    created_at: datetime = T0
    edited_at: datetime | None = None
    author: Author = field(default_factory=Author)
    mentions: list[Author] = field(default_factory=list)
    channel: Chan = field(default_factory=Chan)
    reference: None = None


def raw(*attachments: Attachment, voice: bool = False) -> RawMessage:
    return Raw(list(attachments), Flags(voice))  # type: ignore[return-value]


def kinds(message: RawMessage) -> list[MediaKind]:
    return [ref.kind for ref in media_of(message)]


# --- media_of ------------------------------------------------------------


def test_a_voice_note_is_told_apart_by_the_message_flag() -> None:
    note = Attachment(10, "audio/ogg", "voice-message.ogg", 48_000, duration=6.5)

    assert media_of(raw(note, voice=True)) == (
        MediaRef(
            attachment_id=10,
            kind=MediaKind.VOICE,
            content_type="audio/ogg",
            byte_size=48_000,
            url=note.url,
            filename="voice-message.ogg",
            duration_seconds=6.5,
        ),
    )
    # The same file uploaded, not recorded, is audio.
    assert kinds(raw(note)) == [MediaKind.AUDIO]


@pytest.mark.parametrize(
    "content_type", ["audio/mpeg", "audio/mp4", "audio/wav", "audio/webm", "AUDIO/OGG; codecs=opus"]
)
def test_allowlisted_audio_uploads_are_audio(content_type: str) -> None:
    assert kinds(raw(Attachment(1, content_type))) == [MediaKind.AUDIO]


@pytest.mark.parametrize("content_type", ["image/png", "image/jpeg", "image/webp"])
def test_allowlisted_images_are_images(content_type: str) -> None:
    # A voice flag on the message does not make an image anything else.
    assert kinds(raw(Attachment(1, content_type), voice=True)) == [MediaKind.IMAGE]


@pytest.mark.parametrize(
    "content_type",
    ["image/gif", "video/mp4", "application/pdf", "text/plain", "audio/flac", "", None],
)
def test_anything_else_is_not_recorded(content_type: str | None) -> None:
    assert media_of(raw(Attachment(1, content_type), voice=True)) == ()


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/voice.ogg",
        "http://cdn.discordapp.com/attachments/1/2/voice.ogg",
        "https://cdn.discordapp.com.evil.example/voice.ogg",
    ],
)
def test_an_attachment_off_the_discord_cdn_is_not_recorded(url: str) -> None:
    """The URL is the one thing a later worker would fetch: a host outside the
    closed set is never kept for it."""
    assert media_of(raw(Attachment(1, "audio/ogg", url=url), voice=True)) == ()


def test_each_recordable_attachment_of_a_message_is_kept_in_order() -> None:
    message = raw(
        Attachment(1, "image/png", "a.png"),
        Attachment(2, "application/zip", "b.zip"),
        Attachment(3, "image/jpeg", "c.jpg"),
    )
    assert [ref.attachment_id for ref in media_of(message)] == [1, 3]


def test_a_message_without_attachments_or_flags_has_no_media() -> None:
    """The plain objects the rest of the core is tested with carry neither."""

    @dataclass
    class Bare:
        id: int = 1
        content: str = "hi"
        created_at: datetime = T0
        edited_at: datetime | None = None
        author: Author = field(default_factory=Author)
        mentions: list[Author] = field(default_factory=list)
        channel: Chan = field(default_factory=Chan)
        reference: None = None

    bare: RawMessage = Bare()  # type: ignore[assignment]
    assert media_of(bare) == ()
    message = to_message(bare)
    assert message is not None and message.media == ()


def test_the_converted_message_carries_its_media() -> None:
    message = to_message(raw(Attachment(10, "audio/ogg"), voice=True))
    assert message is not None
    assert [ref.kind for ref in message.media] == [MediaKind.VOICE]


# --- from when ------------------------------------------------------------


def settings(**values: object) -> Settings:
    base = {"discord_token": "t", "discord_guild_id": 1, "database_url": "x", "llm_api_key": "k"}
    return Settings.model_validate({**base, **values})


def test_nothing_is_recorded_until_an_operator_sets_when() -> None:
    assert settings().media_capture_since is None
    # Backfill days alone enable nothing: they count back from a moment.
    assert settings(media_backfill_days=30).media_capture_since is None


def test_recording_starts_at_the_moment_named_or_that_many_days_before() -> None:
    assert settings(media_enabled_at="2026-10-01T00:00:00Z").media_capture_since == datetime(
        2026, 10, 1, tzinfo=UTC
    )
    assert settings(
        media_enabled_at="2026-10-01T00:00:00Z", media_backfill_days=7
    ).media_capture_since == datetime(2026, 10, 1, tzinfo=UTC) - timedelta(days=7)


def test_a_moment_without_a_zone_is_utc() -> None:
    assert settings(media_enabled_at="2026-10-01T00:00:00").media_capture_since == datetime(
        2026, 10, 1, tzinfo=UTC
    )


def test_negative_backfill_days_are_refused_at_boot() -> None:
    with pytest.raises(ValidationError):
        settings(media_enabled_at="2026-10-01T00:00:00Z", media_backfill_days=-1)
