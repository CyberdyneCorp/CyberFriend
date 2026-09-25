"""Storing what people asked the assistant to learn to do.

A suggestion is the person's own words and the ids of where they gave it, and
nothing about the conversation around it. The store is where the bounds live
that a command must not be the only thing enforcing: one row per person and
normalised text, a rolling daily limit decided inside the insert, and no row
at all for somebody who has opted out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from chatmemory.domain.identity import PersonRef

MAX_TEXT_CHARS = 1000
"""The longest suggestion kept. The migration checks the same bound."""

DEFAULT_DAILY_LIMIT = 5
"""Accepted suggestions per person in any rolling 24 hours."""


class SourceKind(StrEnum):
    """How the suggestion arrived."""

    COMMAND = "command"
    DM = "dm"
    CHANNEL = "channel"


class RequestStatus(StrEnum):
    """Where the team is with a suggestion. Set in the admin console."""

    NEW = "new"
    TRIAGED = "triaged"
    PLANNED = "planned"
    DONE = "done"
    DECLINED = "declined"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class SuggestionSource:
    """Where the suggestion was given: ids only, never a channel name."""

    kind: SourceKind
    platform: str
    guild_id: int | None = None
    channel_id: int | None = None


@dataclass(frozen=True, slots=True)
class NewSuggestion:
    """What the service hands the store: validated text and its provenance."""

    person: PersonRef
    text: str
    normalized_hash: bytes
    language: str | None
    source: SuggestionSource
    display_name: str = ""
    """The person's name as the platform shows it, for the team's view."""


@dataclass(frozen=True, slots=True)
class FeatureRequest:
    """One suggestion, as its author sees it."""

    id: int
    text: str
    status: RequestStatus
    created_at: datetime
    notify_on_change: bool = False


class StoreVerdict(StrEnum):
    """What the store did with a submission."""

    STORED = "stored"
    DUPLICATE = "duplicate"
    LIMITED = "limited"
    OPTED_OUT = "opted_out"


@dataclass(frozen=True, slots=True)
class StoreResult:
    verdict: StoreVerdict
    request_id: int | None = None
    """The new row's number, or the existing one's for a duplicate."""


class FeatureRequestStore(Protocol):
    async def submit(
        self, suggestion: NewSuggestion, *, now: datetime, daily_limit: int
    ) -> StoreResult:
        """Store a suggestion unless it is a resubmission, over the limit, or
        from somebody who opted out. A resubmission is answered with the
        existing row's number, before the limit is looked at."""
        ...

    async def for_person(self, person: PersonRef, limit: int) -> Sequence[FeatureRequest]:
        """Their own suggestions, newest first. Never anybody else's."""
        ...

    async def set_notify(self, person: PersonRef, request_id: int, notify: bool) -> bool:
        """Whether to message them on a status change; False when not theirs."""
        ...
