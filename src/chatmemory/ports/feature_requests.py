"""Storing what people asked the assistant to learn to do.

A suggestion is the person's own words and the ids of where they gave it, and
nothing about the conversation around it. The store is where the bounds live
that a command must not be the only thing enforcing: one row per person and
normalised text, a rolling daily limit decided inside the insert under a lock
on the person row, and no row at all for somebody who has opted out.
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
    WEB = "web"
    """Typed in the web user area (0036)."""


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


# --- triage: the team's side -------------------------------------------------------

MAX_NOTE_CHARS = 2000
"""The longest admin note the console keeps."""


@dataclass(frozen=True, slots=True)
class TriageEntry:
    """One suggestion as the team sees it in the console.

    The person's name is read from their person row when the list is asked
    for, so a rename shows and nothing about them is copied onto the row.
    `same_text_elsewhere` counts the *other* people who suggested the same
    normalised text.
    """

    id: int
    text: str
    language: str | None
    status: RequestStatus
    admin_note: str | None
    duplicate_of: int | None
    source_kind: SourceKind
    person_name: str
    same_text_elsewhere: int
    created_at: datetime
    updated_at: datetime
    updated_by: str | None


@dataclass(frozen=True, slots=True)
class TriagePage:
    entries: Sequence[TriageEntry]
    total: int
    """How many suggestions the filter matches, across every page."""


@dataclass(frozen=True, slots=True)
class TriageChange:
    """What an admin asked to change. A field left None is left as it is.

    The note and the duplicate link can be cleared, so each has its own flag
    saying it was given: `set_note` with `admin_note=None` clears the note.
    """

    status: RequestStatus | None = None
    set_note: bool = False
    admin_note: str | None = None
    set_duplicate: bool = False
    duplicate_of: int | None = None

    @property
    def empty(self) -> bool:
        return self.status is None and not self.set_note and not self.set_duplicate


class TriageRefusal(StrEnum):
    NOT_FOUND = "not_found"
    UNKNOWN_DUPLICATE = "unknown_duplicate"
    """`duplicate_of` names a suggestion that does not exist."""


@dataclass(frozen=True, slots=True)
class TriageResult:
    before: TriageEntry | None = None
    after: TriageEntry | None = None
    refusal: TriageRefusal | None = None


class FeatureRequestTriage(Protocol):
    """The console's port: every suggestion, and the admin's changes to one."""

    async def triage_page(
        self, status: RequestStatus | None, *, offset: int, limit: int
    ) -> TriagePage:
        """Newest first, optionally one status only."""
        ...

    async def triage(
        self, request_id: int, change: TriageChange, *, actor: str, now: datetime
    ) -> TriageResult:
        """Apply `change`, recording who made it; the row before and after."""
        ...


# --- status news: telling the author ------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusNews:
    """A status change the author asked to hear about and has not been told.

    Claimed means `notified_status` already holds `status`; `previous` is what
    it held, so an unsent message can be put back.
    """

    request_id: int
    person: PersonRef
    status: RequestStatus
    previous: str
    language: str | None
    text: str


class StatusNewsStore(Protocol):
    async def claim_status_news(self, limit: int) -> Sequence[StatusNews]:
        """Claim changes to tell, for people who opted in to hearing them.

        Skips people who opted out, turned notifications off, or whose direct
        messages are known to be closed; their rows stay unclaimed.
        """
        ...

    async def release(self, news: StatusNews) -> None:
        """Put back a claim whose message was not sent, unless it moved on."""
        ...

    async def record_undeliverable(self, person: PersonRef, now: datetime) -> None:
        """Their direct messages are closed: stop trying until they reopen them."""
        ...
