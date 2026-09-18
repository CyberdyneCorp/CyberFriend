"""How the drain decides what to send, and what it refuses to send.

The queue's own bounds are SQL and are proved in
`tests/integration/test_notifications_queue.py`. What is proved here is the
sequence the delivery service performs, because the sequence is where this
feature can go wrong without any statement being wrong: resolving the viewer
*before* reading what to send, settling what the viewer may no longer read,
and never marking anything sent that was not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from chatmemory.app.notifications import (
    NotificationDelivery,
    NotificationPolicy,
    NotificationPreferences,
    ObligationNotifier,
)
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.notifications import (
    DeliveryResult,
    NotificationDraft,
    NotificationPreference,
    PendingNotification,
    QueueDepth,
    Recipient,
)

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
OPEN_CH = ChannelRef(PLATFORM, 100)
PRIVATE_CH = ChannelRef(PLATFORM, 300)
BOB = PersonRef(PLATFORM, 2)
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)

POLICY = NotificationPolicy(max_items=2)


def an_item(key: str = "k1", channel: ChannelRef = OPEN_CH) -> PendingNotification:
    return PendingNotification(
        ask_key=key,
        channel=channel,
        channel_name="general",
        source_message_id=7,
        kind="request",
        requester_display="alice",
        text="review the migration",
        excerpt="can you review the migration?",
        asked_at=NOW,
    )


@dataclass
class FakeQueue:
    """Records what was asked of it, and under which viewer."""

    pending: dict[PersonRef, list[PendingNotification]] = field(default_factory=dict)
    due: list[Recipient] = field(default_factory=list)
    viewers_read: list[Viewer] = field(default_factory=list)
    settled_unreadable: list[Viewer] = field(default_factory=list)
    sent: list[tuple[PersonRef, tuple[str, ...]]] = field(default_factory=list)
    attempts: list[tuple[PersonRef, datetime]] = field(default_factory=list)
    undeliverable: list[PersonRef] = field(default_factory=list)
    preferences: dict[PersonRef, NotificationPreference] = field(default_factory=dict)
    queued_with: list[tuple[datetime, float, datetime, int]] = field(
        default_factory=list
    )
    withdrawn: int = 0
    expired: int = 0

    async def queue_obligations(
        self, now: datetime, min_confidence: float, not_before: datetime, limit: int
    ) -> int:
        self.queued_with.append((now, min_confidence, not_before, limit))
        return 3

    async def withdraw_settled_asks(self, now: datetime) -> int:
        return self.withdrawn

    async def expire_pending(self, cutoff: datetime, now: datetime) -> int:
        self.expired = 1
        return 1

    async def recipients_due(
        self,
        now: datetime,
        batch_window: timedelta,
        min_interval: timedelta,
        limit: int,
    ) -> Sequence[Recipient]:
        return self.due

    async def pending_for(
        self, viewer: Viewer, limit: int
    ) -> Sequence[PendingNotification]:
        self.viewers_read.append(viewer)
        items = self.pending.get(viewer.person, [])
        return [i for i in items if i.channel in viewer.visible_channels][:limit]

    async def settle_unreadable(self, viewer: Viewer, now: datetime) -> int:
        self.settled_unreadable.append(viewer)
        items = self.pending.get(viewer.person, [])
        return sum(1 for i in items if i.channel not in viewer.visible_channels)

    async def mark_sent(
        self, person: PersonRef, ask_keys: Sequence[str], now: datetime
    ) -> int:
        self.sent.append((person, tuple(ask_keys)))
        return len(ask_keys)

    async def record_attempt(self, person: PersonRef, now: datetime) -> None:
        self.attempts.append((person, now))

    async def record_undeliverable(self, person: PersonRef, now: datetime) -> int:
        self.undeliverable.append(person)
        return 1

    async def set_enabled(
        self, person: PersonRef, enabled: bool
    ) -> NotificationPreference:
        stored = NotificationPreference(enabled=enabled)
        self.preferences[person] = stored
        return stored

    async def preference(self, person: PersonRef) -> NotificationPreference:
        return self.preferences.get(person, NotificationPreference())

    async def depth(self, cap: int) -> QueueDepth:
        return QueueDepth(pending=1, capped=False)


@dataclass
class FakeAcl:
    """Whatever the person may read at the moment they are asked about."""

    visible: dict[PersonRef, frozenset[ChannelRef]] = field(default_factory=dict)

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        return Viewer(
            person=person, visible_channels=self.visible.get(person, frozenset())
        )


@dataclass
class FakeSender:
    result: DeliveryResult = DeliveryResult.SENT
    drafts: list[NotificationDraft] = field(default_factory=list)
    raises: bool = False

    async def send(self, draft: NotificationDraft) -> DeliveryResult:
        if self.raises:
            raise RuntimeError("gateway is having a moment")
        self.drafts.append(draft)
        return self.result


def a_delivery(
    queue: FakeQueue, acl: FakeAcl, sender: FakeSender
) -> NotificationDelivery:
    return NotificationDelivery(queue, acl, sender, POLICY)


# --- batching -----------------------------------------------------------


async def test_several_obligations_become_one_message() -> None:
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)],
        pending={BOB: [an_item("k1"), an_item("k2")]},
    )
    sender = FakeSender()
    acl = FakeAcl({BOB: frozenset({OPEN_CH})})

    report = await a_delivery(queue, acl, sender).deliver(NOW)

    assert len(sender.drafts) == 1
    assert sender.drafts[0].ask_keys == ("k1", "k2")
    assert report.sent == 1
    assert report.items == 2


async def test_a_long_batch_is_capped_and_the_rest_are_counted() -> None:
    """The rest stay queued: nothing is dropped, the message is just shorter."""
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=False)],
        pending={BOB: [an_item(f"k{n}") for n in range(5)]},
    )
    sender = FakeSender()

    await a_delivery(queue, FakeAcl({BOB: frozenset({OPEN_CH})}), sender).deliver(NOW)

    draft = sender.drafts[0]
    assert len(draft.items) == POLICY.max_items
    assert draft.omitted >= 1
    # Only what was named is settled; the rest are still owed.
    assert queue.sent == [(BOB, draft.ask_keys)]


# --- the send-time permission re-check ----------------------------------


async def test_the_viewer_is_resolved_before_anything_is_read() -> None:
    """The ordering is the feature: read under the viewer, not before it."""
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)], pending={BOB: [an_item("k1")]}
    )
    acl = FakeAcl({BOB: frozenset({OPEN_CH})})

    await a_delivery(queue, acl, FakeSender()).deliver(NOW)

    assert [v.person for v in queue.viewers_read] == [BOB]
    assert queue.viewers_read[0].visible_channels == frozenset({OPEN_CH})


async def test_an_obligation_from_a_channel_they_lost_is_not_sent() -> None:
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)],
        pending={BOB: [an_item("k1", PRIVATE_CH)]},
    )
    sender = FakeSender()
    # They can read something, so this is a revocation rather than a cold cache.
    acl = FakeAcl({BOB: frozenset({OPEN_CH})})

    report = await a_delivery(queue, acl, sender).deliver(NOW)

    assert sender.drafts == []
    assert queue.sent == []
    assert report.unreadable == 1
    assert [v.person for v in queue.settled_unreadable] == [BOB]


async def test_only_the_readable_half_of_a_batch_is_sent() -> None:
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)],
        pending={BOB: [an_item("k1", OPEN_CH), an_item("k2", PRIVATE_CH)]},
    )
    sender = FakeSender()

    await a_delivery(queue, FakeAcl({BOB: frozenset({OPEN_CH})}), sender).deliver(NOW)

    assert sender.drafts[0].ask_keys == ("k1",)


async def test_a_cold_guild_cache_sends_and_settles_nothing() -> None:
    """Nothing readable is a reconnect as often as a departure."""
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)], pending={BOB: [an_item("k1")]}
    )
    sender = FakeSender()

    report = await a_delivery(queue, FakeAcl(), sender).deliver(NOW)

    assert sender.drafts == []
    assert queue.settled_unreadable == []
    assert queue.viewers_read == []
    assert report.nothing_to_say == 1


# --- how to stop --------------------------------------------------------


async def test_the_first_message_to_somebody_says_how_to_stop() -> None:
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)], pending={BOB: [an_item("k1")]}
    )
    sender = FakeSender()

    await a_delivery(queue, FakeAcl({BOB: frozenset({OPEN_CH})}), sender).deliver(NOW)

    assert sender.drafts[0].say_how_to_stop is True


async def test_later_messages_do_not_repeat_it() -> None:
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=False)], pending={BOB: [an_item("k1")]}
    )
    sender = FakeSender()

    await a_delivery(queue, FakeAcl({BOB: frozenset({OPEN_CH})}), sender).deliver(NOW)

    assert sender.drafts[0].say_how_to_stop is False


# --- delivery failure ---------------------------------------------------


async def test_a_closed_direct_message_is_recorded_and_not_marked_sent() -> None:
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)], pending={BOB: [an_item("k1")]}
    )
    sender = FakeSender(result=DeliveryResult.CLOSED)

    report = await a_delivery(queue, FakeAcl({BOB: frozenset({OPEN_CH})}), sender).deliver(NOW)

    assert queue.undeliverable == [BOB]
    assert queue.sent == []
    assert report.closed == 1


async def test_a_transient_failure_leaves_the_batch_queued() -> None:
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)], pending={BOB: [an_item("k1")]}
    )
    sender = FakeSender(result=DeliveryResult.FAILED)

    report = await a_delivery(queue, FakeAcl({BOB: frozenset({OPEN_CH})}), sender).deliver(NOW)

    assert queue.sent == []
    assert queue.undeliverable == []
    assert report.failed == 1


async def test_a_failed_delivery_is_stamped_so_the_rate_limit_covers_it() -> None:
    """The regression: a failure that records nothing is a retry loop.

    Nothing is settled -- the obligation is still owed -- but the attempt is
    stamped, because the claim statement's rate limit is the only thing
    standing between a failing delivery and the drain interval. The sender
    here is the realistic failure: a batch the platform renders as two
    messages, whose second call fails after the first has already reached the
    person.
    """
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)], pending={BOB: [an_item("k1")]}
    )
    sender = FakeSender(result=DeliveryResult.FAILED)

    await a_delivery(queue, FakeAcl({BOB: frozenset({OPEN_CH})}), sender).deliver(NOW)

    assert queue.attempts == [(BOB, NOW)]
    # Still owed, and still unsent: the stamp delays the retry, it does not
    # pretend the message arrived.
    assert queue.sent == []
    assert queue.undeliverable == []


async def test_a_send_that_raises_is_stamped_like_any_other_failure() -> None:
    """A gateway that throws is the same event as one that reports failure."""
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)], pending={BOB: [an_item("k1")]}
    )

    report = await a_delivery(
        queue, FakeAcl({BOB: frozenset({OPEN_CH})}), FakeSender(raises=True)
    ).deliver(NOW)

    assert queue.attempts == [(BOB, NOW)]
    assert report.failed == 1


async def test_one_unreachable_person_does_not_end_the_pass() -> None:
    queue = FakeQueue(
        due=[Recipient(BOB, first_time=True)], pending={BOB: [an_item("k1")]}
    )
    sender = FakeSender(raises=True)

    report = await a_delivery(queue, FakeAcl({BOB: frozenset({OPEN_CH})}), sender).deliver(NOW)

    assert report.failed == 1
    assert queue.sent == []


# --- the sweep ----------------------------------------------------------


async def test_the_sweep_applies_the_age_bound_and_the_threshold() -> None:
    queue = FakeQueue()
    policy = NotificationPolicy(max_age=timedelta(hours=6), min_confidence=0.7)

    await ObligationNotifier(queue, policy).sweep(NOW)

    [(now, confidence, not_before, _limit)] = queue.queued_with
    assert now == NOW
    assert confidence == 0.7
    assert not_before == NOW - timedelta(hours=6)


async def test_the_sweep_reports_what_it_did() -> None:
    queue = FakeQueue(withdrawn=2)

    report = await ObligationNotifier(queue, POLICY).sweep(NOW)

    assert report.queued == 3
    assert report.withdrawn == 2
    assert report.expired == 1
    assert report.as_dict()["pending"] == 1


# --- the person's own switch --------------------------------------------


async def test_turning_notifications_off_and_on_again() -> None:
    queue = FakeQueue()
    preferences = NotificationPreferences(queue)

    off = await preferences.turn_off(BOB)
    on = await preferences.turn_on(BOB)

    assert off.enabled is False
    assert on.enabled is True
    assert (await preferences.current(BOB)).enabled is True
