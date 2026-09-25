"""Conversation as stored.

`Message` is the canonical record; `Window` is a rebuildable projection over
messages used as the unit of semantic retrieval. Individual chat messages are
too short to carry embedding signal, so similarity is computed over windows
and citations resolve back to the messages inside them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.media import MediaRef


@dataclass(frozen=True, slots=True)
class Message:
    platform_message_id: int
    channel: ChannelRef
    author: PersonRef
    content: str
    created_at: datetime
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    reply_to_id: int | None = None
    thread_id: int | None = None
    mentions: frozenset[PersonRef] = field(default_factory=frozenset)
    # How the author should be shown to a reader. Carried on the message
    # because it is what the platform said at the time; the canonical person
    # keeps the latest one it has seen.
    author_display: str = ""
    # The voice notes and images it carries, as the platform described them.
    # Empty on a message read back from the store: only capture sets it.
    media: tuple[MediaRef, ...] = ()

    @property
    def is_visible(self) -> bool:
        return self.deleted_at is None

    @property
    def revision(self) -> datetime:
        """The value deduplication keys on, alongside the message id.

        Deliberately a timestamp and never a content hash: comparing a hash of
        parsed text against one computed over raw text puts the two in
        different domains, so they never match and every sync reprocesses
        everything while appearing to work.
        """
        return self.edited_at or self.created_at


@dataclass(frozen=True, slots=True)
class Window:
    """A group of messages treated as one retrieval unit."""

    channel: ChannelRef
    message_ids: tuple[int, ...]
    text: str
    starts_at: datetime
    ends_at: datetime
    thread_id: int | None = None
    window_id: int | None = None

    @property
    def is_empty(self) -> bool:
        return not self.message_ids


@dataclass(frozen=True, slots=True)
class DirtyChannel:
    """A channel whose windows need re-forming, and the mark's generation.

    The generation exists so the rebuild can clear the mark only if nothing
    was marked while it ran. Marks fold in with LEAST, so a later change does
    not move the watermark and would otherwise be cleared without ever being
    applied.
    """

    channel: ChannelRef
    since: datetime
    generation: int
