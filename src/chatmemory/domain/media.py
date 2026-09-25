"""Voice notes and images attached to a captured message.

A `MediaRef` is what the platform said about an attachment -- never its bytes.
Capture records one per attachment of a kind the deployment may later
transcribe or describe, as a pending row keyed to its message, so everything
that removes the message (deletion, retention, opt-out, a channel leaving
scope) removes the row with it.

Nothing here is verified: the type, size and duration are the uploading
client's word, and whatever later processes the row checks the bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MediaKind(StrEnum):
    VOICE = "voice"
    """Recorded in the Discord client: the message carries IS_VOICE_MESSAGE."""
    AUDIO = "audio"
    """An uploaded audio file."""
    IMAGE = "image"


AUDIO_TYPES = frozenset({"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/webm"})
"""Declared audio types captured. Ogg is what a Discord voice note is; the rest
are the uploads a transcription endpoint accepts."""

IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
"""Declared image types captured. GIF and video are out of scope."""


@dataclass(frozen=True, slots=True)
class MediaRef:
    """One attachment, as the platform described it."""

    attachment_id: int
    kind: MediaKind
    content_type: str
    byte_size: int
    url: str
    """A signed CDN URL that expires; refreshed each time the message is re-read."""
    filename: str = ""
    duration_seconds: float | None = None
    """Set by Discord on voice notes only."""
