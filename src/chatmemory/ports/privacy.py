"""Reading everything the assistant holds about one person, for `/privacy`.

One read across every store of personal data, keyed on the person and nothing
else, so the only data it can return is the asker's own. Channel content is
read only for the channels the caller says the person can read now: archived
messages and their media elsewhere are neither counted nor named, because a
count is already a disclosure that such channels exist (`channel_listing`).

The inventory carries values -- fact values, the person's own questions, task
texts -- because the direct-message view shows them. The guild view never sees
them: it is rendered from `Inventory.summary()`, a type with no field that could
hold a value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import IntEnum, StrEnum
from typing import Protocol

from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.facts import FactKind

RECENT_QUESTIONS = 5
"""How many of the person's latest remembered questions the DM view shows."""


@dataclass(frozen=True, slots=True)
class HeldFact:
    kind: FactKind
    value: str


@dataclass(frozen=True, slots=True)
class MemoryCounts:
    """Remembered conversation, split by where it happened.

    Direct messages and server channels, not channel by channel: a channel the
    person asked in may be one they can no longer read, and naming it would say
    it exists.
    """

    direct_turns: int = 0
    direct_summaries: int = 0
    channel_turns: int = 0
    channel_summaries: int = 0

    @property
    def total(self) -> int:
        return (
            self.direct_turns + self.direct_summaries + self.channel_turns + self.channel_summaries
        )


@dataclass(frozen=True, slots=True)
class HeldTask:
    id: int
    question: str
    interval_hours: int
    last_outcome: str | None
    disabled: bool


@dataclass(frozen=True, slots=True)
class HeldAlert:
    id: int
    kind: str
    chain: str | None
    address: str | None
    asset: str | None


@dataclass(frozen=True, slots=True)
class NotificationSetting:
    """`enabled` is None when the person never chose; the default is on."""

    enabled: bool | None = None
    queued: int = 0


@dataclass(frozen=True, slots=True)
class MediaCounts:
    """Attachments on the person's archived messages in readable channels.

    `by_kind` counts rows per media kind (voice, audio, image); `with_text`
    counts the ones a transcript or description was stored for.
    """

    by_kind: tuple[tuple[str, int], ...] = ()
    with_text: int = 0

    @property
    def total(self) -> int:
        return sum(count for _, count in self.by_kind)


@dataclass(frozen=True, slots=True)
class HeldSuggestion:
    id: int
    text: str
    status: str


@dataclass(frozen=True, slots=True)
class HeldToken:
    label: str | None
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class ArchivedChannel:
    """A channel the person can read now, and how many of their messages it holds."""

    channel: ChannelRef
    messages: int


@dataclass(frozen=True, slots=True)
class Inventory:
    """Everything held about one person, values included. DM only."""

    known: bool = False
    """False when no person row exists: nothing has ever been stored."""
    platforms: tuple[str, ...] = ()
    archiving: bool = True
    """False once the person opted out: nothing they send is archived."""
    facts: tuple[HeldFact, ...] = ()
    memory: MemoryCounts = MemoryCounts()
    recent_questions: tuple[str, ...] = ()
    tasks: tuple[HeldTask, ...] = ()
    alerts: tuple[HeldAlert, ...] = ()
    notifications: NotificationSetting = NotificationSetting()
    voice_seconds_this_month: int = 0
    media: MediaCounts = MediaCounts()
    suggestions: tuple[HeldSuggestion, ...] = ()
    tokens: tuple[HeldToken, ...] = ()
    archived: tuple[ArchivedChannel, ...] = ()
    traces: int = 0
    """Exported traces of questions the person asked that are not deleted yet."""

    def summary(self) -> InventorySummary:
        """What a server channel may show: counts and fact kinds, no values."""
        return InventorySummary(
            known=self.known,
            platforms=self.platforms,
            archiving=self.archiving,
            fact_kinds=tuple(dict.fromkeys(f.kind for f in self.facts)),
            memory=self.memory,
            tasks=len(self.tasks),
            alerts=len(self.alerts),
            notifications=self.notifications,
            voice_seconds_this_month=self.voice_seconds_this_month,
            media=self.media.total,
            suggestions=len(self.suggestions),
            tokens=len(self.tokens),
            archived=self.archived,
            traces=self.traces,
        )


@dataclass(frozen=True, slots=True)
class InventorySummary:
    """The guild view's input. Deliberately has nowhere to put a value."""

    known: bool
    platforms: tuple[str, ...]
    archiving: bool
    fact_kinds: tuple[FactKind, ...]
    memory: MemoryCounts
    tasks: int
    alerts: int
    notifications: NotificationSetting
    voice_seconds_this_month: int
    media: int
    suggestions: int
    tokens: int
    archived: tuple[ArchivedChannel, ...]
    traces: int


class PrivacyStore(Protocol):
    async def inventory(
        self, person: PersonRef, readable_channel_ids: Sequence[int], month: date
    ) -> Inventory:
        """Everything held about `person`, reading channel content only in
        `readable_channel_ids`. An unknown person is `Inventory()`, and is not
        created: reading must not invent people."""
        ...


# --- delete everything ---------------------------------------------------------


ERASED_NAME = "(erased)"
"""The display name an erased person's record keeps. Not their account id, so
the unnamed-people repair never looks their name up again."""


class ErasureMode(StrEnum):
    """The two buttons: delete everything, or delete everything and leave."""

    ERASE = "erase"
    ERASE_AND_OPT_OUT = "erase_and_opt_out"


class ErasureStep(IntEnum):
    """The last step of an erasure that finished, as `erasure_request.step` holds it.

    Numbered as the design numbers them. Steps 3 and 4 (traces quoting their
    messages, traces of their questions) are one call and finish together.
    """

    RECORDED = 1
    REIMPORT_STOPPED = 2
    TRACES_MARKED = 4
    PURGED = 5
    VOICE_FOLDED = 6
    TOMBSTONED = 7
    COMPLETE = 8


@dataclass(frozen=True, slots=True)
class ErasureCounts:
    """What the reply reports, taken from the inventory before anything goes.

    `messages` and `media` count only channels the person could read when they
    confirmed, as the dashboard does: a count of the rest would say that
    channels they cannot see are archived. Everything of theirs is deleted
    regardless. `traces` is filled in by the trace step, as "at least".
    """

    messages: int = 0
    media: int = 0
    facts: int = 0
    memory: int = 0
    tasks: int = 0
    alerts: int = 0
    suggestions: int = 0
    tokens: int = 0
    voice_seconds: int = 0
    traces: int = 0

    @classmethod
    def of(cls, inventory: Inventory) -> ErasureCounts:
        return cls(
            messages=sum(a.messages for a in inventory.archived),
            media=inventory.media.total,
            facts=len(inventory.facts),
            memory=inventory.memory.total,
            tasks=len(inventory.tasks),
            alerts=len(inventory.alerts),
            suggestions=len(inventory.suggestions),
            tokens=len(inventory.tokens),
            voice_seconds=inventory.voice_seconds_this_month,
        )

    def as_json(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> ErasureCounts:
        return cls(**{k: int(str(v)) for k, v in data.items() if k in cls.__slots__})


@dataclass(frozen=True, slots=True)
class ErasureRequest:
    id: int
    person_id: int
    person: PersonRef
    mode: ErasureMode
    step: ErasureStep
    requested_at: datetime
    counts: ErasureCounts = ErasureCounts()

    @property
    def complete(self) -> bool:
        return self.step is ErasureStep.COMPLETE


class ErasureStore(Protocol):
    """The durable request and the steps that are plain SQL.

    Every step method is idempotent: a resumed erasure repeats the step it was
    in when the process stopped.
    """

    async def open(
        self, person: PersonRef, mode: ErasureMode, counts: ErasureCounts
    ) -> ErasureRequest:
        """Record the request, creating the person row if unseen. With one
        already open for the person, that one is returned; a request to also
        stop archiving upgrades it and repeats its steps."""
        ...

    async def stop_reimport(self, request: ErasureRequest) -> None:
        """Set `person.erased_before` to the request time (never earlier than it was)."""
        ...

    async def record_traces(self, request: ErasureRequest, traces: int) -> None: ...

    async def purge_derived(self, request: ErasureRequest) -> None:
        """`purge_person_derived(person_id)`."""
        ...

    async def fold_voice(self, request: ErasureRequest) -> int:
        """Move the person's `media_usage` seconds into the anonymous total;
        returns the seconds moved."""
        ...

    async def tombstone(self, request: ErasureRequest) -> None:
        """Clear the name and preferences, keeping id, platform ids, `erased_before`."""
        ...

    async def advance(self, request: ErasureRequest, step: ErasureStep) -> ErasureRequest: ...

    async def open_requests(self, idle_since: datetime, limit: int) -> Sequence[ErasureRequest]:
        """Open requests with no step finished since `idle_since`, oldest first."""
        ...
