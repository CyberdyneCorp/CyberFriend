"""Ports for the messages the assistant sends without being asked.

Two halves of one feature that never share a process. Obligations are
extracted where ingestion runs; Discord is reachable only where the bot holds
its gateway connection. So the queue is the seam, and it is a port rather than
a shared object: one side writes rows, the other drains them, and access can
change in between.

The asymmetry from `app.asks.ports` is repeated here, and matters more. Every
method that returns something to *send* takes a `Viewer` as a required
positional argument, and the viewer is resolved from live guild state at the
moment of sending -- not the moment of extraction. A notification is an
unsolicited message about a conversation, so the permission that let it be
queued is not the permission that lets it be delivered.

There is deliberately no method that returns another person's pending
notifications, and no method that takes a person and returns content. "Show me
what Hezron has been told he owes" is not expressible through this port.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer


class NotificationOutcome(StrEnum):
    """How a queued notification settled. Mirrors `OUTCOMES` in migration 0015.

    Kept rather than deleted, because "why was I never told about this?" is
    the question this feature will be asked, and a removed row answers it with
    silence.
    """

    SENT = "sent"
    #: The recipient could no longer read the source channel at send time.
    UNREADABLE = "unreadable"
    #: The ask was answered or corrected before anybody was told about it.
    WITHDRAWN = "withdrawn"
    #: Their direct messages are closed.
    UNDELIVERABLE = "undeliverable"
    #: It sat in the queue past its shelf life.
    EXPIRED = "expired"


class DeliveryResult(StrEnum):
    """What happened when the platform was asked to deliver one message."""

    SENT = "sent"
    #: Refused because the person does not accept direct messages from us.
    #: Distinct from FAILED on purpose: this one must stop the retrying.
    CLOSED = "closed"
    #: Anything transient -- a timeout, a 500, a rate limit. Tried again.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Recipient:
    """Somebody with pending notifications who is due a message.

    Carries no content. This is the first half of the two-step drain: who is
    due is decided without a viewer, because it is a question about queue
    rows and preferences rather than about anything anyone said. What they are
    told is decided in the second half, under their viewer.
    """

    person: PersonRef
    #: Whether we have ever messaged them before. The first message has to say
    #: how to stop, and that has to be a fact rather than a guess.
    first_time: bool


@dataclass(frozen=True, slots=True)
class PendingNotification:
    """One obligation the recipient may still read the source of.

    Only ever produced by a read that bound the recipient's *current* readable
    channels, so holding one of these is already the permission check having
    passed.
    """

    ask_key: str
    channel: ChannelRef
    channel_name: str
    source_message_id: int
    #: The ask's kind, as `AskKind` spells it. A string here rather than
    #: the enum because a port may know the domain and nothing above it,
    #: and `AskKind` is part of the ask pipeline rather than of either.
    kind: str
    requester_display: str
    #: The extracted obligation, in the model's words.
    text: str
    #: A short quotation of the source message. Content, and therefore data.
    excerpt: str
    asked_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationPreference:
    """What one person has said about being messaged, and what we learned."""

    enabled: bool = True
    undeliverable: bool = False

    @property
    def deliverable(self) -> bool:
        return self.enabled and not self.undeliverable


@dataclass(frozen=True, slots=True)
class QueueDepth:
    """Queue health, as two numbers with no content in them."""

    pending: int = 0
    capped: bool = False


class NotificationQueue(Protocol):
    """The seam between extraction and delivery."""

    async def queue_obligations(
        self,
        now: datetime,
        min_confidence: float,
        not_before: datetime,
        limit: int,
    ) -> int:
        """Queue obligations addressed to an individual. Returns rows added.

        Idempotent: the row is keyed by the ask, so a sweep that runs twice
        over the same extraction queues one notification, and a settled row is
        never re-queued. `not_before` is what keeps a first deployment, or a
        backlog pass reading a year of history, from messaging everybody about
        things they were asked months ago.
        """
        ...

    async def withdraw_settled_asks(self, now: datetime) -> int:
        """Settle pending rows whose ask is no longer outstanding.

        Somebody who ticks the message within the batching window is never
        messaged about it at all, which is the difference between a useful
        reminder and nagging.
        """
        ...

    async def expire_pending(self, cutoff: datetime, now: datetime) -> int:
        """Settle pending rows older than `cutoff`, so none is sent very late."""
        ...

    async def recipients_due(
        self,
        now: datetime,
        batch_window: timedelta,
        min_interval: timedelta,
        limit: int,
    ) -> Sequence[Recipient]:
        """Who is due a batch: opted in, not rate-limited, reachable.

        Returns identities only. Opt-out, the person's own switch, the closed
        -DM record and the rate limit are all predicates in the statement
        behind this, never checks a caller could forget to perform.
        """
        ...

    async def pending_for(
        self, viewer: Viewer, limit: int
    ) -> Sequence[PendingNotification]:
        """The viewer's pending notifications, from channels they may read NOW.

        The viewer is required and is resolved immediately before this call:
        the readable set is bound into the statement's WHERE clause, so an
        obligation from a channel they have lost access to is not returned
        rather than returned and then filtered.
        """
        ...

    async def settle_unreadable(self, viewer: Viewer, now: datetime) -> int:
        """Settle the viewer's pending rows they may no longer read.

        The complement of `pending_for` under the same bound set. Without it a
        person who leaves a channel keeps coming up as due forever, and the
        drain pass spends every iteration deciding to send them nothing.
        """
        ...

    async def mark_sent(
        self, person: PersonRef, ask_keys: Sequence[str], now: datetime
    ) -> int:
        """Record delivery, and start the person's rate-limit interval."""
        ...

    async def record_attempt(self, person: PersonRef, now: datetime) -> None:
        """Record a delivery that was attempted and did not succeed.

        The rate limit has to bound attempts and not only deliveries. A send
        that failed may still have put a message in front of the person -- a
        batch rendered as two platform messages whose second call fails has
        already delivered its first, and an accepted send whose response was
        lost looks identical from here. Without this the failing delivery
        settles nothing and stamps nothing, so the next drain pass seconds
        later picks the same person up again and the only bound left is how
        long the queue rows live.

        Deliberately not `mark_sent`: nothing is settled, so the obligations
        are still owed and are tried again once the interval has passed, and
        nothing records that this person has ever been messaged -- so the
        first message that does land still says how to stop.
        """
        ...

    async def record_undeliverable(self, person: PersonRef, now: datetime) -> int:
        """Record that their direct messages are closed, and stop retrying."""
        ...

    async def set_enabled(self, person: PersonRef, enabled: bool) -> NotificationPreference:
        """Turn notifications off, or back on. Returns the preference stored."""
        ...

    async def preference(self, person: PersonRef) -> NotificationPreference:
        """What the person has chosen. Their own, keyed by their account."""
        ...

    async def depth(self, cap: int) -> QueueDepth:
        """How much is waiting, bounded. A count, with no content in it."""
        ...


@dataclass(frozen=True, slots=True)
class NotificationDraft:
    """One batched message, before a platform has rendered it.

    Structured rather than a string because escaping, link masking and the
    wording of "how to stop" are all platform concerns, and the app layer may
    not import a platform library. What the app decides is *what is said*: to
    whom, about which obligations, and whether this person has to be told how
    to stop.
    """

    person: PersonRef
    items: tuple[PendingNotification, ...]
    #: Obligations left out because the message would otherwise be a wall.
    omitted: int = 0
    #: True only on the first message this person has ever been sent.
    say_how_to_stop: bool = False

    @property
    def ask_keys(self) -> tuple[str, ...]:
        return tuple(item.ask_key for item in self.items)


class NotificationSender(Protocol):
    """Delivers one drafted message, and says what the platform did with it."""

    async def send(self, draft: NotificationDraft) -> DeliveryResult: ...
