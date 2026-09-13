"""What an ask is, and the vocabulary the rest of the package shares.

An ask is a claim the *system* made about people -- "Hezron is waiting on Leo"
is inference, not something anyone typed -- so every field that could be
guessed is instead explicit: the addressee has an `UNATTRIBUTED` value, the
confidence is carried rather than thresholded away at write time, and the
status names the observable event that produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message


class AskKind(StrEnum):
    """The three things worth extracting, kept distinct.

    They are answered differently: a REQUEST and a QUESTION are owed *to*
    someone else, a COMMITMENT is owed *by* the speaker, and conflating them
    makes "what do I need to do" either miss promises or invent duties.
    """

    REQUEST = "request"
    QUESTION = "question"
    COMMITMENT = "commitment"


class AskStatus(StrEnum):
    """State reached by observation only.

    There is deliberately no CLOSED-by-ageing value: an ask nobody answered in
    three weeks is STALE, and hiding it would hide exactly what the person was
    asking about.
    """

    OPEN = "open"
    ANSWERED = "answered"
    STALE = "stale"


#: Statuses that still represent something outstanding.
OUTSTANDING = (AskStatus.OPEN, AskStatus.STALE)


class ClosedBy(StrEnum):
    """The observable event that closed an ask. Never a model's judgement."""

    REPLY = "reply"
    REACTION = "reaction"


class AddresseeKind(StrEnum):
    PERSON = "person"
    GROUP = "group"
    UNATTRIBUTED = "unattributed"


class CorrectionResolution(StrEnum):
    DONE = "done"
    NOT_APPLICABLE = "not_applicable"


class AddresseeSignal(StrEnum):
    """Why a message was considered worth extracting from.

    Recorded rather than discarded because it is the cost control: extraction
    runs on messages carrying one of these, and knowing which one fired is how
    the filter is tuned without lowering quality.
    """

    MENTION = "mention"
    REPLY = "reply"
    BOT_DM = "bot_dm"
    SECOND_PERSON = "second_person"
    #: First-person commitment ("I'll take that"). The addressee of a promise
    #: is the person making it, so a commitment is plausibly addressed even
    #: when it mentions nobody -- and without this signal every commitment
    #: made into an empty room is invisible to "what do I need to do".
    FIRST_PERSON = "first_person"


@dataclass(frozen=True, slots=True)
class Addressee:
    """Who an ask fell to -- including "we could not tell"."""

    kind: AddresseeKind
    person: PersonRef | None = None
    group: str | None = None

    def __post_init__(self) -> None:
        # The constructor is where guessing would enter, so it is where the
        # shape is enforced: a person id may exist only when a person was
        # actually resolved.
        if (self.kind is AddresseeKind.PERSON) != (self.person is not None):
            raise ValueError(
                "a person addressee requires a person, and nothing else may carry one"
            )
        if (self.kind is AddresseeKind.GROUP) != (self.group is not None):
            raise ValueError(
                "a group addressee requires a group label, and nothing else may carry one"
            )

    @property
    def is_attributed(self) -> bool:
        return self.kind is AddresseeKind.PERSON

    @property
    def slug(self) -> str:
        """The addressee's contribution to the ask key. Stable across runs."""
        if self.person is not None:
            return f"person:{self.person.platform}:{self.person.platform_user_id}"
        if self.group is not None:
            return f"group:{_slug(self.group)}"
        return "unattributed"


UNATTRIBUTED = Addressee(kind=AddresseeKind.UNATTRIBUTED)


def to_person(person: PersonRef) -> Addressee:
    return Addressee(kind=AddresseeKind.PERSON, person=person)


def to_group(label: str) -> Addressee:
    return Addressee(kind=AddresseeKind.GROUP, group=label.strip() or "group")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40] or "group"


def ask_key(source_message_id: int, kind: AskKind, addressee: Addressee) -> str:
    """Identity for an extracted ask.

    Deliberately derived from the source rather than from the extracted text:
    models disagree with themselves across runs, and a key over generated text
    would recreate every ask as a new obligation on each pass -- including ones
    the addressee had already dismissed.
    """
    return f"{source_message_id}:{kind.value}:{addressee.slug}"


@dataclass(frozen=True, slots=True)
class AskCandidate:
    """A message worth paying a model to read, and what it is read against."""

    message: Message
    signal: AddresseeSignal
    context: tuple[Message, ...] = ()
    reply_parent: Message | None = None

    @property
    def channel(self) -> ChannelRef:
        return self.message.channel


@dataclass(frozen=True, slots=True)
class ExtractedAsk:
    """One model output, before any of it is trusted.

    `addressee_hint` is whatever the model believed it was told -- a name, a
    team, or nothing. It is a *hint* because resolution, not the model, decides
    who an ask is recorded against.
    """

    kind: AskKind
    text: str
    confidence: float
    addressee_hint: str | None = None
    addressee_is_group: bool = False


@dataclass(frozen=True, slots=True)
class Ask:
    """An extracted obligation as stored."""

    key: str
    source_message_id: int
    channel: ChannelRef
    requester: PersonRef
    addressee: Addressee
    kind: AskKind
    text: str
    confidence: float
    asked_at: datetime
    status: AskStatus = AskStatus.OPEN
    thread_id: int | None = None
    closed_at: datetime | None = None
    closed_by: ClosedBy | None = None
    ask_id: int | None = None

    @property
    def is_outstanding(self) -> bool:
        return self.status in OUTSTANDING


@dataclass(frozen=True, slots=True)
class Correction:
    """A person's statement about their own ask. Outranks extraction."""

    ask_key: str
    by: PersonRef
    resolution: CorrectionResolution


class CorrectionOutcome(StrEnum):
    APPLIED = "applied"
    #: Rejected because the corrector is not the addressee. Allowing this would
    #: make closing someone else's obligations a way to hide them.
    NOT_ADDRESSEE = "not_addressee"
    UNKNOWN_ASK = "unknown_ask"


@dataclass(frozen=True, slots=True)
class ObligationRequest:
    """Intent only.

    There is no channel, viewer or permission field here, and there must never
    be one: the same reasoning as for `SearchQuery`. Who the asks belong to is
    taken from the viewer at the store boundary, so asking about someone else's
    obligations is not expressible.
    """

    since: datetime | None = None
    until: datetime | None = None
    kinds: frozenset[AskKind] = field(default_factory=lambda: frozenset(AskKind))
    include_commitments: bool = True
    statuses: tuple[AskStatus, ...] = OUTSTANDING
    #: Presentation threshold. Not authorization: lowering it surfaces weaker
    #: extractions from channels the viewer can already read, never other
    #: people's channels.
    min_confidence: float = 0.0
    limit: int = 50


@dataclass(frozen=True, slots=True)
class StateRefresh:
    """What one pass of observable state transitions changed."""

    answered_by_reply: int = 0
    answered_by_reaction: int = 0
    marked_stale: int = 0

    @property
    def total(self) -> int:
        return self.answered_by_reply + self.answered_by_reaction + self.marked_stale


@dataclass(frozen=True, slots=True)
class ReportedAsk:
    """An ask together with the evidence a reader needs to check it."""

    ask: Ask
    requester_display: str
    source_excerpt: str


@dataclass(frozen=True, slots=True)
class AskPolicy:
    """The thresholds this feature is tuned by.

    `min_confidence` gates presentation, not storage: a sub-threshold ask is
    still recorded, because knowing the extractor produced it is how the
    threshold gets tuned. It simply never reaches an answer.
    """

    min_confidence: float = 0.6
    stale_after: timedelta = timedelta(days=21)
    excerpt_chars: int = 240

    def presentable(self, confidence: float) -> bool:
        return confidence >= self.min_confidence
