"""What the notification queue does, against real SQL.

Every bound on messaging somebody who asked for nothing is a WHERE clause, so
a fake queue written by the same hand as the service proves nothing about any
of them. These run the statements.

The two that matter most are the two the change's own risk section names: an
obligation addressed to a group notifies nobody, and access revoked between
extraction and sending stops the notification. Both are asserted here as
properties of the database rather than of a code path somebody could stop
calling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.adapters.store.notify_postgres import PostgresNotificationQueue
from chatmemory.app.asks.model import Ask as AskRecord
from chatmemory.app.asks.model import (
    AskKind,
    Correction,
    CorrectionResolution,
    ask_key,
    to_group,
    to_person,
)
from chatmemory.app.notifications import (
    NotificationDelivery,
    NotificationPolicy,
    NotificationPreferences,
    ObligationNotifier,
)
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.notifications import DeliveryResult, NotificationDraft

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
OPEN_CH = ChannelRef(PLATFORM, 100)
PRIVATE_CH = ChannelRef(PLATFORM, 300)

ALICE = PersonRef(PLATFORM, 1)
BOB = PersonRef(PLATFORM, 2)

T0 = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=10)

POLICY = NotificationPolicy(
    batch_window=timedelta(minutes=5),
    min_interval=timedelta(hours=1),
    max_age=timedelta(hours=24),
    expire_after=timedelta(days=3),
    max_items=10,
)


@pytest.fixture(autouse=True)
async def _requires_notification_schema(clean: AsyncEngine) -> None:
    async with clean.connect() as conn:
        present = await conn.execute(text("SELECT to_regclass('public.notification')"))
        if present.scalar() is None:
            pytest.skip("notification tables are missing; run `alembic upgrade head`")


def viewer(person: PersonRef, *channels: ChannelRef) -> Viewer:
    return Viewer(person=person, visible_channels=frozenset(channels))


async def seed_corpus(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for channel, name in ((OPEN_CH, "general"), (PRIVATE_CH, "leadership")):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT DO NOTHING"
                ),
                {"id": channel.platform_channel_id, "n": name},
            )
        for person, name in ((ALICE, "alice"), (BOB, "bob")):
            created = await conn.execute(
                text("INSERT INTO person (display_name) VALUES (:n) RETURNING id"),
                {"n": name},
            )
            await conn.execute(
                text(
                    "INSERT INTO person_platform_id "
                    "(platform, platform_user_id, person_id) VALUES (:p, :u, :i)"
                ),
                {
                    "p": PLATFORM,
                    "u": person.platform_user_id,
                    "i": created.scalar_one(),
                },
            )


async def add_message(
    engine: AsyncEngine,
    message_id: int,
    author: PersonRef,
    channel: ChannelRef,
    content: str = "can you review the migration before friday?",
    at: datetime | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO message (id, channel_id, author_person_id, content, "
                "created_at) SELECT :id, :c, p.person_id, :content, :at "
                "FROM person_platform_id p "
                "WHERE p.platform = :platform AND p.platform_user_id = :author"
            ),
            {
                "id": message_id,
                "c": channel.platform_channel_id,
                "content": content,
                "at": at or T0,
                "platform": PLATFORM,
                "author": author.platform_user_id,
            },
        )


def an_ask(
    source_message_id: int,
    channel: ChannelRef = OPEN_CH,
    requester: PersonRef = ALICE,
    addressee: PersonRef | None = BOB,
    kind: AskKind = AskKind.REQUEST,
    confidence: float = 0.9,
    asked_at: datetime | None = None,
) -> AskRecord:
    target = to_person(addressee) if addressee is not None else to_group("the team")
    return AskRecord(
        key=ask_key(source_message_id, kind, target),
        source_message_id=source_message_id,
        channel=channel,
        requester=requester,
        addressee=target,
        kind=kind,
        text="review the migration",
        confidence=confidence,
        asked_at=asked_at or T0,
    )


async def extracted(
    engine: AsyncEngine,
    message_id: int = 1,
    channel: ChannelRef = OPEN_CH,
    addressee: PersonRef | None = BOB,
    requester: PersonRef = ALICE,
    confidence: float = 0.9,
    asked_at: datetime | None = None,
) -> AskRecord:
    """One message and the ask extracted from it, as ingest would leave them."""
    await add_message(engine, message_id, requester, channel, at=asked_at or T0)
    ask = an_ask(
        message_id,
        channel=channel,
        requester=requester,
        addressee=addressee,
        confidence=confidence,
        asked_at=asked_at,
    )
    await PostgresAskStore(engine).record_asks(message_id, [ask])
    return ask


async def pending_rows(engine: AsyncEngine) -> list[tuple[str, str | None]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT ask_key, outcome FROM notification ORDER BY ask_key")
        )
        return [(str(r[0]), r[1]) for r in rows]


# --- what may be queued at all -----------------------------------------


async def test_an_obligation_addressed_to_a_person_is_queued(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    ask = await extracted(clean)

    queued = await ObligationNotifier(
        PostgresNotificationQueue(clean), POLICY
    ).sweep(NOW)

    assert queued.queued == 1
    assert await pending_rows(clean) == [(ask.key, None)]


async def test_an_obligation_addressed_to_a_group_notifies_nobody(
    clean: AsyncEngine,
) -> None:
    """The scenario the spec names, and the structural reason it holds.

    A group-addressed ask carries no addressee person id, and the queue's
    `person_id` is NOT NULL: there is no row shape a group obligation could
    take, so this cannot be reintroduced by a change to the enqueue predicate
    alone.
    """
    await seed_corpus(clean)
    await extracted(clean, addressee=None)

    swept = await ObligationNotifier(PostgresNotificationQueue(clean), POLICY).sweep(NOW)

    assert swept.queued == 0
    assert await pending_rows(clean) == []


async def test_nobody_is_notified_of_their_own_commitment(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await extracted(clean, addressee=ALICE, requester=ALICE)

    swept = await ObligationNotifier(PostgresNotificationQueue(clean), POLICY).sweep(NOW)

    assert swept.queued == 0


async def test_an_obligation_older_than_the_age_bound_is_never_queued(
    clean: AsyncEngine,
) -> None:
    """The bound that makes this safe to deploy onto a server with history.

    The backlog extraction pass reads a year of imported archive. Without this
    the first sweep after a deploy messages everybody about everything they
    were ever asked.
    """
    await seed_corpus(clean)
    await extracted(clean, asked_at=NOW - timedelta(days=30))

    swept = await ObligationNotifier(PostgresNotificationQueue(clean), POLICY).sweep(NOW)

    assert swept.queued == 0


async def test_a_weak_extraction_is_not_sent_unasked(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await extracted(clean, confidence=0.2)

    swept = await ObligationNotifier(PostgresNotificationQueue(clean), POLICY).sweep(NOW)

    assert swept.queued == 0


async def test_an_opted_out_person_is_never_queued(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await extracted(clean)
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO person_opt_out (person_id, reason) "
                "SELECT person_id, 'asked' FROM person_platform_id "
                "WHERE platform = :p AND platform_user_id = :u"
            ),
            {"p": PLATFORM, "u": BOB.platform_user_id},
        )

    swept = await ObligationNotifier(PostgresNotificationQueue(clean), POLICY).sweep(NOW)

    assert swept.queued == 0
    assert await pending_rows(clean) == []


async def test_opting_out_empties_a_queue_already_filled(clean: AsyncEngine) -> None:
    """The flag and the purge share a transaction, as they do for memory."""
    await seed_corpus(clean)
    await extracted(clean)
    await ObligationNotifier(PostgresNotificationQueue(clean), POLICY).sweep(NOW)
    assert len(await pending_rows(clean)) == 1

    async with clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO person_opt_out (person_id, reason) "
                "SELECT person_id, 'asked' FROM person_platform_id "
                "WHERE platform = :p AND platform_user_id = :u"
            ),
            {"p": PLATFORM, "u": BOB.platform_user_id},
        )

    assert await pending_rows(clean) == []


async def test_sweeping_twice_queues_one_notification(clean: AsyncEngine) -> None:
    """Keyed by the ask, so re-extraction and a repeated sweep both cost one."""
    await seed_corpus(clean)
    ask = await extracted(clean)
    notifier = ObligationNotifier(PostgresNotificationQueue(clean), POLICY)

    first = await notifier.sweep(NOW)
    second = await notifier.sweep(NOW + timedelta(minutes=1))

    assert (first.queued, second.queued) == (1, 0)
    assert await pending_rows(clean) == [(ask.key, None)]


async def test_a_delivered_notification_is_never_queued_again(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    ask = await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)
    await queue.mark_sent(BOB, [ask.key], NOW)

    again = await ObligationNotifier(queue, POLICY).sweep(NOW + timedelta(minutes=1))

    assert again.queued == 0
    assert await pending_rows(clean) == [(ask.key, "sent")]


# --- what is withdrawn before anybody is told ---------------------------


async def test_an_ask_answered_inside_the_batching_window_is_withdrawn(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    ask = await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    async with clean.begin() as conn:
        await conn.execute(
            text("UPDATE ask SET status = 'answered' WHERE ask_key = :k"),
            {"k": ask.key},
        )
    swept = await ObligationNotifier(queue, POLICY).sweep(NOW + timedelta(minutes=1))

    assert swept.withdrawn == 1
    assert await pending_rows(clean) == [(ask.key, "withdrawn")]


async def test_a_dismissed_ask_is_withdrawn(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    ask = await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    await PostgresAskStore(clean).apply_correction(
        viewer(BOB, OPEN_CH),
        Correction(ask.key, BOB, CorrectionResolution.NOT_APPLICABLE),
    )
    swept = await ObligationNotifier(queue, POLICY).sweep(NOW + timedelta(minutes=1))

    assert swept.withdrawn == 1


async def test_a_notification_nobody_could_be_sent_expires(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    swept = await ObligationNotifier(queue, POLICY).sweep(NOW + timedelta(days=4))

    assert swept.expired == 1
    assert [outcome for _, outcome in await pending_rows(clean)] == ["expired"]


# --- who is due ---------------------------------------------------------


async def test_the_addressee_is_due_once_the_batching_window_has_passed(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    too_early = await queue.recipients_due(
        NOW, POLICY.batch_window, POLICY.min_interval, 10
    )
    later = await queue.recipients_due(
        NOW + timedelta(minutes=6), POLICY.batch_window, POLICY.min_interval, 10
    )

    assert too_early == []
    assert [r.person for r in later] == [BOB]
    assert later[0].first_time is True


async def test_somebody_who_turned_them_off_is_never_due(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)
    await queue.set_enabled(BOB, False)

    due = await queue.recipients_due(
        NOW + timedelta(hours=1), POLICY.batch_window, POLICY.min_interval, 10
    )

    assert due == []


async def test_turning_them_back_on_makes_the_waiting_batch_due_again(
    clean: AsyncEngine,
) -> None:
    """Nothing was dropped while they were off; it waited."""
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)
    await queue.set_enabled(BOB, False)
    await queue.set_enabled(BOB, True)

    due = await queue.recipients_due(
        NOW + timedelta(hours=1), POLICY.batch_window, POLICY.min_interval, 10
    )

    assert [r.person for r in due] == [BOB]


async def test_the_rate_makes_a_second_batch_wait_rather_than_be_dropped(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    first = await extracted(clean, message_id=1)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)
    sent_at = NOW + timedelta(minutes=6)
    await queue.mark_sent(BOB, [first.key], sent_at)

    await extracted(clean, message_id=2, asked_at=sent_at)
    await ObligationNotifier(queue, POLICY).sweep(sent_at + timedelta(minutes=1))

    inside_the_rate = await queue.recipients_due(
        sent_at + timedelta(minutes=10), POLICY.batch_window, POLICY.min_interval, 10
    )
    after_the_rate = await queue.recipients_due(
        sent_at + timedelta(hours=2), POLICY.batch_window, POLICY.min_interval, 10
    )

    assert inside_the_rate == []
    assert [r.person for r in after_the_rate] == [BOB]
    # Waiting, not dropped: the row is still pending.
    assert (an_ask(2).key, None) in await pending_rows(clean)


async def test_a_person_messaged_before_is_not_told_how_to_stop_again(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    first = await extracted(clean, message_id=1)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)
    sent_at = NOW + timedelta(minutes=6)
    await queue.mark_sent(BOB, [first.key], sent_at)

    await extracted(clean, message_id=2, asked_at=sent_at)
    await ObligationNotifier(queue, POLICY).sweep(sent_at + timedelta(minutes=1))
    due = await queue.recipients_due(
        sent_at + timedelta(hours=2), POLICY.batch_window, POLICY.min_interval, 10
    )

    assert [r.first_time for r in due] == [False]


async def test_somebody_whose_direct_messages_are_closed_is_not_retried(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    settled = await queue.record_undeliverable(BOB, NOW)
    due = await queue.recipients_due(
        NOW + timedelta(hours=2), POLICY.batch_window, POLICY.min_interval, 10
    )

    assert settled == 1
    assert due == []
    assert [outcome for _, outcome in await pending_rows(clean)] == ["undeliverable"]


async def test_turning_notifications_back_on_clears_a_closed_direct_message(
    clean: AsyncEngine,
) -> None:
    """Somebody typing a command at us is somebody we have a channel to."""
    await seed_corpus(clean)
    queue = PostgresNotificationQueue(clean)
    await queue.record_undeliverable(BOB, NOW)

    assert (await queue.preference(BOB)).deliverable is False
    assert (await queue.set_enabled(BOB, True)).deliverable is True


# --- the send-time permission re-check ----------------------------------


async def test_access_revoked_between_extraction_and_sending_stops_it(
    clean: AsyncEngine,
) -> None:
    """The scenario the spec names.

    The obligation is extracted from a channel the addressee could read, and
    the viewer resolved at send time no longer includes it. Nothing about the
    queue row changed -- only the recipient's permissions did.
    """
    await seed_corpus(clean)
    await extracted(clean, channel=PRIVATE_CH)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    while_they_could_read_it = await queue.pending_for(viewer(BOB, PRIVATE_CH), 10)
    after_revocation = await queue.pending_for(viewer(BOB, OPEN_CH), 10)

    assert len(while_they_could_read_it) == 1
    assert after_revocation == []


async def test_what_they_may_no_longer_read_is_settled_unsent(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await extracted(clean, channel=PRIVATE_CH)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    settled = await queue.settle_unreadable(viewer(BOB, OPEN_CH), NOW)

    assert settled == 1
    assert [outcome for _, outcome in await pending_rows(clean)] == ["unreadable"]


async def test_a_cold_guild_cache_settles_nothing(clean: AsyncEngine) -> None:
    """A viewer with nothing readable is a reconnect as often as a departure.

    Settling on it would discard everybody's queue during a gateway blip.
    """
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    settled = await queue.settle_unreadable(viewer(BOB), NOW)

    assert settled == 0
    assert [outcome for _, outcome in await pending_rows(clean)] == [None]


async def test_a_notification_carries_the_channel_the_asker_and_an_excerpt(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    [item] = await queue.pending_for(viewer(BOB, OPEN_CH), 10)

    assert item.channel == OPEN_CH
    assert item.channel_name == "general"
    assert item.requester_display == "alice"
    assert item.source_message_id == 1
    assert "review the migration" in item.excerpt


async def test_a_deleted_source_message_is_not_notified_about(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)
    async with clean.begin() as conn:
        await conn.execute(text("UPDATE message SET deleted_at = now() WHERE id = 1"))

    assert await queue.pending_for(viewer(BOB, OPEN_CH), 10) == []


async def test_nobody_elses_queue_is_readable_through_the_viewer(
    clean: AsyncEngine,
) -> None:
    """The viewer decides whose notifications these are, not a caller."""
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    assert await queue.pending_for(viewer(ALICE, OPEN_CH), 10) == []


# --- a delivery that failed ---------------------------------------------


@dataclass
class _FixedAcl:
    """Whatever the recipient may read, as the gateway would report it."""

    channels: frozenset[ChannelRef]

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        return Viewer(person=person, visible_channels=self.channels)


@dataclass
class _HalfDeliveredSender:
    """The realistic failure: piece one lands, piece two does not.

    `DiscordNotificationSender` renders a batch into as many messages as the
    platform's size limit requires and sends them one at a time, so a failure
    on the second call has already put the first in front of the person. It
    reports FAILED, because from the port's side that is all that is known.
    """

    delivered: int = 0

    async def send(self, draft: NotificationDraft) -> DeliveryResult:
        self.delivered += 1
        return DeliveryResult.FAILED


async def test_a_failed_delivery_is_not_retried_until_the_rate_allows_it(
    clean: AsyncEngine,
) -> None:
    """The regression, against the statement that actually decides.

    A failed send settles nothing and once stamped nothing, so `RECIPIENTS_DUE`
    returned the same person on the very next drain pass -- fifteen seconds
    later, for as long as the rows lived. With a partial send that is a
    duplicate direct message every fifteen seconds for three days, which is
    exactly the unsolicited messaging the rate limit exists to bound.
    """
    await seed_corpus(clean)
    for message_id in range(1, 11):
        await extracted(clean, message_id=message_id)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)
    sender = _HalfDeliveredSender()
    delivery = NotificationDelivery(
        queue, _FixedAcl(frozenset({OPEN_CH})), sender, POLICY
    )

    at = NOW + timedelta(minutes=6)
    for _ in range(12):  # three simulated minutes of the drain loop
        await delivery.deliver(at)
        at += timedelta(seconds=15)

    assert sender.delivered == 1
    # Nothing was settled: the obligations are still owed, and are tried again
    # once the interval has passed rather than fifteen seconds later.
    assert [outcome for _, outcome in await pending_rows(clean)] == [None] * 10
    after_the_rate = await queue.recipients_due(
        at + POLICY.min_interval, POLICY.batch_window, POLICY.min_interval, 10
    )
    assert [r.person for r in after_the_rate] == [BOB]


async def test_a_failed_delivery_does_not_consume_the_first_message(
    clean: AsyncEngine,
) -> None:
    """A failure is no evidence anybody was messaged, so the way out survives.

    If a failed attempt stamped `first_notified_at` as well, the first message
    that actually landed would be the first one not to say how to stop.
    """
    await seed_corpus(clean)
    await extracted(clean)
    queue = PostgresNotificationQueue(clean)
    await ObligationNotifier(queue, POLICY).sweep(NOW)

    await queue.record_attempt(BOB, NOW + timedelta(minutes=6))
    due = await queue.recipients_due(
        NOW + timedelta(hours=2), POLICY.batch_window, POLICY.min_interval, 10
    )

    assert [r.first_time for r in due] == [True]


async def test_a_failed_delivery_to_an_unknown_account_creates_nobody(
    clean: AsyncEngine,
) -> None:
    """Deciding not to message somebody must not invent them."""
    queue = PostgresNotificationQueue(clean)

    await queue.record_attempt(PersonRef(PLATFORM, 9999), NOW)

    async with clean.connect() as conn:
        people = await conn.execute(
            text("SELECT count(*) FROM person_platform_id WHERE platform_user_id = 9999")
        )
        assert people.scalar_one() == 0


# --- stopping them mid-pass ---------------------------------------------


CARA = PersonRef(PLATFORM, 3)


async def seed_person(engine: AsyncEngine, person: PersonRef, name: str) -> None:
    async with engine.begin() as conn:
        created = await conn.execute(
            text("INSERT INTO person (display_name) VALUES (:n) RETURNING id"),
            {"n": name},
        )
        await conn.execute(
            text(
                "INSERT INTO person_platform_id "
                "(platform, platform_user_id, person_id) VALUES (:p, :u, :i)"
            ),
            {"p": PLATFORM, "u": person.platform_user_id, "i": created.scalar_one()},
        )


@dataclass
class _StopsWhileTheBatchIsRunning:
    """Somebody types `/notifications off` part-way through a drain pass.

    The pass claims up to `recipients_per_pass` identities in one query and
    then walks them one at a time, and every iteration costs an ACL resolve,
    a user lookup and at least one direct message. So the gap between "you are
    due" and "here is your message" is seconds wide for the people at the back
    of the batch, and this sender stands in for a command handler running in
    that gap.
    """

    queue: PostgresNotificationQueue
    stop: PersonRef
    messaged: list[PersonRef] = field(default_factory=list)

    async def send(self, draft: NotificationDraft) -> DeliveryResult:
        self.messaged.append(draft.person)
        await NotificationPreferences(self.queue).turn_off(self.stop)
        return DeliveryResult.SENT


async def test_turning_them_off_during_a_pass_stops_the_message(
    clean: AsyncEngine,
) -> None:
    """The regression, against the statement that decides what is said.

    `RECIPIENTS_DUE` read the person's switch once per pass; nothing re-read
    it afterwards, and `PENDING_FOR_VIEWER` -- the last read before a direct
    message goes out -- carried no predicate on it. So somebody who turned
    notifications off a second into a pass got the ephemeral confirmation and
    then the message anyway. Their obligations still wait: the switch stops
    the message, it does not discard what is owed.
    """
    await seed_corpus(clean)
    await seed_person(clean, CARA, "cara")
    queue = PostgresNotificationQueue(clean)
    notifier = ObligationNotifier(queue, POLICY)
    # Two sweeps, so `min(queued_at)` orders the claim: Bob is messaged
    # first and Cara is the person at the back of the batch.
    await extracted(clean, message_id=1, addressee=BOB)
    await notifier.sweep(NOW)
    await extracted(clean, message_id=2, addressee=CARA)
    await notifier.sweep(NOW + timedelta(seconds=1))

    sender = _StopsWhileTheBatchIsRunning(queue=queue, stop=CARA)
    delivery = NotificationDelivery(
        queue, _FixedAcl(frozenset({OPEN_CH})), sender, POLICY
    )
    report = await delivery.deliver(NOW + timedelta(minutes=6))

    assert sender.messaged == [BOB]
    assert report.sent == 1
    # Nothing of Cara's was dropped, and she is not due again while she is off.
    async with clean.connect() as conn:
        settled = await conn.execute(
            text(
                "SELECT p.platform_user_id, n.outcome FROM notification n "
                "JOIN person_platform_id p ON p.person_id = n.person_id "
                "ORDER BY p.platform_user_id"
            )
        )
        assert [(int(r[0]), r[1]) for r in settled] == [
            (BOB.platform_user_id, "sent"),
            (CARA.platform_user_id, None),
        ]
    assert (await queue.preference(CARA)).enabled is False
