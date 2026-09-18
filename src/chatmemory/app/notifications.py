"""Telling somebody an obligation was addressed to them, and stopping.

Every other surface in this system waits to be asked. This one does not, and
that is a different relationship with the people in the server: a message
nobody requested is the assistant spending their attention rather than its
own. So the feature is bounded on every side, and the bounds are structural
rather than advisory.

*Only the person an obligation names.* Never a group, never a channel, never
anybody who was mentioned in passing. Group-addressed asks carry no addressee
person id at all, so the enqueue statement cannot produce a row for one.

*Batched, and rate-limited.* An obligation waits out a batching window before
anybody is told, so a busy morning is one message; a person is messaged at
most once per configured interval, and a delivery that failed spends that
interval too -- a failed send may still have arrived, and retrying one every
few seconds is how an assistant becomes the thing it was told to stop being.
Reaching the limit makes pending notifications wait, never drops them -- the
queue row is the record that something is owed, and discarding it would lose
the obligation, not the message.

*Stoppable, and never to somebody who opted out.* One command turns them off.
The exclusion list is a predicate in every statement, plus a trigger, because
an unsolicited message to somebody who withdrew from the corpus is the worst
thing this feature could do.

*Only what they can still read.* Extraction and delivery are separated by a
queue and access changes in between, so the recipient's readable channels are
resolved from live guild state immediately before the message is composed and
bound into the read as a predicate. This mirrors conversation memory
re-checking a remembered turn, and for the same reason: a permission change
must not be outrun by a delivery.

The two halves never share a process. `ObligationNotifier` runs where
extraction runs -- the ingest process -- and only writes queue rows.
`NotificationDelivery` runs where Discord is reachable -- the bot -- and only
drains them. Neither imports the other; the queue is the whole of the
coupling.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import structlog

from chatmemory.domain.identity import PersonRef
from chatmemory.ports.acl import AclResolver
from chatmemory.ports.notifications import (
    DeliveryResult,
    NotificationDraft,
    NotificationPreference,
    NotificationQueue,
    NotificationSender,
    Recipient,
)

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    """The numbers this feature is tuned by. Guesses until people use it.

    `max_age` is the one that is not a comfort setting. The backlog extraction
    pass reads a year of imported history, and every ask it finds is new to
    the queue; without a floor on how old an obligation may be, the first
    sweep after a deploy would send everybody in the server a message about
    everything they were ever asked. It is a deployment safety bound, and it
    is applied in SQL.
    """

    #: How long an obligation waits for the others that will join its message.
    batch_window: timedelta = timedelta(minutes=5)
    #: The rate: at most one message per person per interval.
    min_interval: timedelta = timedelta(hours=1)
    #: No notification is ever queued for an obligation older than this.
    max_age: timedelta = timedelta(hours=24)
    #: A notification nobody could be sent by now is no longer news.
    expire_after: timedelta = timedelta(days=3)
    #: Obligations named in one message. The rest are counted, not listed.
    max_items: int = 10
    #: People messaged in one drain pass.
    recipients_per_pass: int = 20
    #: Obligations queued in one sweep. Bounds a burst, not the total.
    queued_per_sweep: int = 200
    #: The confidence the answer path refuses to report below. An extraction
    #: too weak to list on request is far too weak to send unasked.
    min_confidence: float = 0.6

    def not_before(self, now: datetime) -> datetime:
        return now - self.max_age

    def expiry(self, now: datetime) -> datetime:
        return now - self.expire_after


@dataclass(frozen=True, slots=True)
class SweepReport:
    """One pass of the enqueue sweep, for the health endpoint."""

    queued: int = 0
    withdrawn: int = 0
    expired: int = 0
    pending: int = 0
    pending_capped: bool = False
    last_run_at: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "queued": self.queued,
            "withdrawn": self.withdrawn,
            "expired": self.expired,
            "pending": self.pending,
            "pending_capped": self.pending_capped,
            "last_run_at": self.last_run_at,
        }


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """One pass of the drain, for the health endpoint.

    `unreadable` is the counter worth watching: it is how often somebody would
    have been told about a channel they can no longer read, which is the thing
    the send-time re-check exists to prevent.
    """

    considered: int = 0
    sent: int = 0
    items: int = 0
    unreadable: int = 0
    nothing_to_say: int = 0
    closed: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "considered": self.considered,
            "sent": self.sent,
            "items": self.items,
            "unreadable": self.unreadable,
            "nothing_to_say": self.nothing_to_say,
            "closed": self.closed,
            "failed": self.failed,
        }


#: How much of the queue is counted for health. A figure, not a total.
DEPTH_CAP = 1_000

#: How far past the message's own cap one batch is read, so that what was left
#: out can be counted rather than merely hinted at. A floor and not a total:
#: somebody with more than this waiting is told about the ones that fit and a
#: number that under-states the rest, which is the right direction to be wrong
#: in for a message nobody asked for.
OMITTED_PROBE = 50


class ObligationNotifier:
    """The ingest half: turns extracted obligations into queue rows.

    Deliberately not called from extraction itself. Extraction writes asks
    through a store this does not own, and the live pass and the backlog pass
    both produce them; a sweep over the ask table catches both, cannot be
    forgotten by a future third writer, and is idempotent because the queue
    row is keyed by the ask. It is the same arrangement as the extraction
    watermark -- one pass records, another finds what is left.
    """

    def __init__(
        self, queue: NotificationQueue, policy: NotificationPolicy | None = None
    ) -> None:
        self._queue = queue
        self._policy = policy or NotificationPolicy()

    async def sweep(self, now: datetime) -> SweepReport:
        """Queue what is new, and settle what will never be sent."""
        policy = self._policy
        queued = await self._queue.queue_obligations(
            now=now,
            min_confidence=policy.min_confidence,
            not_before=policy.not_before(now),
            limit=policy.queued_per_sweep,
        )
        # Before anything is sent rather than after: somebody who replies or
        # ticks inside the batching window is never messaged at all.
        withdrawn = await self._queue.withdraw_settled_asks(now)
        expired = await self._queue.expire_pending(policy.expiry(now), now)
        depth = await self._queue.depth(DEPTH_CAP)
        if queued or withdrawn or expired:
            log.info(
                "notifications.swept",
                queued=queued,
                withdrawn=withdrawn,
                expired=expired,
                pending=depth.pending,
            )
        return SweepReport(
            queued=queued,
            withdrawn=withdrawn,
            expired=expired,
            pending=depth.pending,
            pending_capped=depth.capped,
        )


class NotificationDelivery:
    """The bot half: drains the queue through whatever can reach a person.

    The order here is the feature. Who is due comes back with no content in
    it; then, and only then, the recipient's readable channels are resolved
    from live guild state and bound into the read that decides what they are
    told. A permission that has been revoked since extraction removes the
    obligation from the message by removing it from the query.
    """

    def __init__(
        self,
        queue: NotificationQueue,
        acl: AclResolver,
        sender: NotificationSender,
        policy: NotificationPolicy | None = None,
    ) -> None:
        self._queue = queue
        self._acl = acl
        self._sender = sender
        self._policy = policy or NotificationPolicy()

    async def deliver(self, now: datetime) -> DeliveryReport:
        """Send one batch to each person who is due one."""
        recipients = await self._queue.recipients_due(
            now=now,
            batch_window=self._policy.batch_window,
            min_interval=self._policy.min_interval,
            limit=self._policy.recipients_per_pass,
        )
        report = DeliveryReport(considered=len(recipients))
        for recipient in recipients:
            report = await self._deliver_to(recipient, now, report)
        return report

    async def _deliver_to(
        self, recipient: Recipient, now: datetime, report: DeliveryReport
    ) -> DeliveryReport:
        person = recipient.person
        # Resolved now, not at extraction. This is the send-time re-check, and
        # everything below is bounded by it.
        viewer = await self._acl.resolve_viewer(person)
        if not viewer.visible_channels:
            # Either they left the server or the guild cache is cold, and the
            # two look identical from here. Nothing is sent and nothing is
            # settled: a reconnect must not discard everybody's queue.
            log.info("notifications.viewer_unresolved", person=str(person))
            return replace(report, nothing_to_say=report.nothing_to_say + 1)

        items = await self._queue.pending_for(
            viewer, self._policy.max_items + OMITTED_PROBE
        )
        # The complement, under the same readable set: obligations from
        # channels they have lost access to are settled unsent rather than
        # left to make this person due for ever.
        dropped = await self._queue.settle_unreadable(viewer, now)
        if dropped:
            log.info(
                "notifications.access_revoked_before_sending",
                person=str(person),
                dropped=dropped,
            )
            report = replace(report, unreadable=report.unreadable + dropped)

        if not items:
            return replace(report, nothing_to_say=report.nothing_to_say + 1)

        shown = tuple(items[: self._policy.max_items])
        draft = NotificationDraft(
            person=person,
            items=shown,
            omitted=max(0, len(items) - len(shown)),
            say_how_to_stop=recipient.first_time,
        )
        return await self._send(draft, now, report)

    async def _send(
        self, draft: NotificationDraft, now: datetime, report: DeliveryReport
    ) -> DeliveryReport:
        try:
            result = await self._sender.send(draft)
        except Exception:
            # One unreachable person must not end the pass. Treated exactly
            # like a reported failure below, because from here the two are the
            # same event: the platform was asked to deliver and did not say it
            # had.
            log.exception("notifications.send_failed", person=str(draft.person))
            result = DeliveryResult.FAILED

        if result is DeliveryResult.CLOSED:
            # Their direct messages are closed. Recorded, their queue settled,
            # and they are excluded from every later pass by predicate: a bot
            # retrying a closed DM forever is how it gets rate-limited into
            # uselessness, and they have effectively already said no.
            settled = await self._queue.record_undeliverable(draft.person, now)
            log.info(
                "notifications.direct_messages_closed",
                person=str(draft.person),
                settled=settled,
            )
            return replace(report, closed=report.closed + 1)

        if result is DeliveryResult.FAILED:
            # The rows stay pending -- nothing is owed any less because a send
            # failed -- but the attempt is stamped, and the rate limit reads
            # that stamp. Without it the only record of this pass is the
            # counter in the report, the queue looks exactly as it did a
            # moment ago, and the next drain pass picks this person up again
            # fifteen seconds later: the rate limit would be enforced only on
            # the path where nothing went wrong. It matters most where the
            # failure is partial, because a batch that the platform renders as
            # more than one message can fail after the first has landed, and
            # so can a send whose response is lost after it was accepted.
            # Retrying every fifteen seconds until the queue expires is the
            # unwanted messaging this whole module is written to prevent.
            await self._queue.record_attempt(draft.person, now)
            log.info("notifications.delivery_attempt_failed", person=str(draft.person))
            return replace(report, failed=report.failed + 1)

        await self._queue.mark_sent(draft.person, draft.ask_keys, now)
        log.info(
            "notifications.sent",
            person=str(draft.person),
            items=len(draft.items),
            first_time=draft.say_how_to_stop,
        )
        return replace(
            report, sent=report.sent + 1, items=report.items + len(draft.items)
        )


class NotificationPreferences:
    """A person's own switch, over their authenticated account and no other.

    Separate from delivery because it is reachable from the command surface
    and delivery is not: the only thing a person can do to this feature is
    turn it off and on, and that has to work whether or not anything is
    currently queued for them.
    """

    def __init__(self, queue: NotificationQueue) -> None:
        self._queue = queue

    async def turn_off(self, person: PersonRef) -> NotificationPreference:
        log.info("notifications.turned_off", person=str(person))
        return await self._queue.set_enabled(person, False)

    async def turn_on(self, person: PersonRef) -> NotificationPreference:
        log.info("notifications.turned_on", person=str(person))
        return await self._queue.set_enabled(person, True)

    async def current(self, person: PersonRef) -> NotificationPreference:
        return await self._queue.preference(person)
