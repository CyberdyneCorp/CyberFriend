"""SQL for the notification queue, and the two places its bounds live.

Every bound on an unsolicited message is a predicate here rather than a check
somebody performs before calling. There are seven of them and they are all in
WHERE clauses:

  addressed to an individual   `a.addressee_kind = 'person'`, at enqueue
  never to somebody opted out  `NOT EXISTS (... person_opt_out ...)`, all three
  never to somebody who said stop  `pref.enabled`, at claim and again at send
  never to closed direct messages  `pref.undeliverable_at IS NULL`, both
  not more often than the rate  `pref.last_notified_at <= :rate_ready`, at claim
  nor more often than that in failed attempts  `pref.last_attempt_at`, at claim
  only what they may read NOW  `n.channel_id = ANY(:channel_ids)`, at send

The bounds that say "and again at send" are there because a drain pass is not
instantaneous: it claims a batch of identities in one query and then messages
them one at a time, so the person at the back of the batch is claimed seconds
before their direct message is composed. Anything a person can change about
being messaged -- their switch, their withdrawal from the corpus -- is
therefore re-read in the statement that decides what they are told, which is
the last database read before anything is sent.

The last one is the reason this file is split the way it is. Deciding *who* is
due is a question about queue rows and preferences, with no viewer and no
content in the answer; deciding *what they are told* is a read of conversation
content, and it binds the recipient's readable channel set exactly as the
corpus and the ask store do. Extraction and delivery are separated by this
queue, and access changes in between, so the set bound at `PENDING_FOR_VIEWER`
is resolved from live guild state immediately before the message is sent --
not the set that was in force when the obligation was extracted.

`SETTLE_UNREADABLE` is the complement of that read on the channel bound --
the rows it could never return -- and it is not an optimisation: without it
somebody who leaves a channel is due forever, and every drain pass rediscovers
that it has nothing to say to them.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

# --- identity ----------------------------------------------------------

RESOLVE_PERSON_ID = text("""
SELECT person_id FROM person_platform_id
WHERE platform = :platform AND platform_user_id = :platform_user_id
""")

# --- the extraction side -----------------------------------------------

#: Queue the obligations nobody has been told about yet.
#:
#: Modelled on the extraction watermark: the row *is* the mark. It is keyed by
#: `ask_key`, so re-extraction of the same message queues nothing new, a sweep
#: that runs twice queues one notification, and a settled row is never
#: reconsidered -- there is no "sent" flag to forget to set, because the
#: presence of the row is the flag.
#:
#: `:not_before` is the bound that makes this safe to deploy onto a server with
#: history. The backlog pass reads a year of archive; without a floor on
#: `asked_at`, the first sweep after a deploy would message everybody about
#: everything they were ever asked.
QUEUE_OBLIGATIONS = text("""
INSERT INTO notification (ask_key, person_id, channel_id, source_message_id, queued_at)
SELECT a.ask_key, a.addressee_person_id, a.channel_id, a.source_message_id, :now
FROM ask a
JOIN message m ON m.id = a.source_message_id AND m.deleted_at IS NULL
WHERE a.addressee_kind = 'person'
  -- Structural, not stylistic: an obligation naming a group has no addressee
  -- person, so a group-addressed ask cannot produce a row here at all.
  AND a.addressee_person_id IS NOT NULL
  -- Nobody is told they asked themselves something. A commitment is recorded
  -- against the person who made it, and a direct message about a promise
  -- somebody made thirty seconds ago is the assistant talking to itself.
  AND a.addressee_person_id <> a.requester_person_id
  AND a.status = 'open'
  -- The same threshold the answer path refuses to report below. An extraction
  -- too weak to show in a list somebody asked for is far too weak to send
  -- unasked.
  AND a.confidence >= :min_confidence
  AND a.asked_at >= CAST(:not_before AS timestamptz)
  AND NOT EXISTS (SELECT 1 FROM ask_correction c WHERE c.ask_key = a.ask_key)
  AND NOT EXISTS (
      SELECT 1 FROM person_opt_out o WHERE o.person_id = a.addressee_person_id
  )
  AND NOT EXISTS (SELECT 1 FROM notification n WHERE n.ask_key = a.ask_key)
ORDER BY a.asked_at
LIMIT :limit
ON CONFLICT (ask_key) DO NOTHING
""")

#: Drop what has already been dealt with. An ask answered or dismissed inside
#: the batching window produces no message at all, which is the difference
#: between a reminder and nagging.
WITHDRAW_SETTLED = text("""
UPDATE notification n SET settled_at = :now, outcome = 'withdrawn'
FROM ask a
WHERE a.ask_key = n.ask_key
  AND n.settled_at IS NULL
  AND (
      a.status NOT IN ('open', 'stale')
      OR EXISTS (SELECT 1 FROM ask_correction c WHERE c.ask_key = n.ask_key)
  )
""")

#: A notification nobody could be sent in three days is no longer news, and a
#: queue with no floor grows for ever. Settled rather than deleted so the
#: record still says why nothing arrived.
EXPIRE_PENDING = text("""
UPDATE notification SET settled_at = :now, outcome = 'expired'
WHERE settled_at IS NULL AND queued_at < CAST(:cutoff AS timestamptz)
""")

# --- the delivery side -------------------------------------------------

#: Who is due a batch. Identities only: no channel, no text, no ask.
#:
#: Every reason not to message somebody is a predicate here, so there is no
#: code path in which one of them is skipped. `:batch_ready` is now minus the
#: batching window, which is what makes several obligations in quick
#: succession arrive as one message; `:rate_ready` is now minus the minimum
#: interval, which is the rate limit. A person the rate excludes keeps their
#: pending rows -- nothing is dropped, it waits.
RECIPIENTS_DUE = text("""
SELECT n.person_id,
       p.platform_user_id,
       bool_and(pref.first_notified_at IS NULL) AS first_time
FROM notification n
JOIN person_platform_id p
  ON p.person_id = n.person_id AND p.platform = :platform
LEFT JOIN notification_preference pref ON pref.person_id = n.person_id
WHERE n.settled_at IS NULL
  AND n.queued_at <= CAST(:batch_ready AS timestamptz)
  -- No preference row means nobody has ever expressed one. Every other bound
  -- has already been applied by the time this is read, so the default is on.
  AND COALESCE(pref.enabled, TRUE)
  AND pref.undeliverable_at IS NULL
  AND (
      pref.last_notified_at IS NULL
      OR pref.last_notified_at <= CAST(:rate_ready AS timestamptz)
  )
  -- The same interval over attempts that did *not* succeed, because a
  -- delivery that failed may still have reached the person: a batch split
  -- into two Discord messages whose second call fails has already delivered
  -- its first, and a send whose response was lost is indistinguishable from
  -- one that never arrived. Without this predicate a failed delivery settles
  -- nothing and stamps nothing, so this person is selected again on the next
  -- drain pass seconds later and keeps being selected until the queue rows
  -- expire -- which is the rate limit being enforced only when it is not
  -- needed.
  AND (
      pref.last_attempt_at IS NULL
      OR pref.last_attempt_at <= CAST(:rate_ready AS timestamptz)
  )
  AND NOT EXISTS (
      SELECT 1 FROM person_opt_out o WHERE o.person_id = n.person_id
  )
GROUP BY n.person_id, p.platform_user_id
ORDER BY min(n.queued_at)
LIMIT :limit
""")

#: What one person is told, bound by what they may read at this moment.
#:
#: `:channel_ids` is the recipient's readable set, resolved from live guild
#: state immediately before sending. It is the whole of the send-time
#: permission re-check, and it is a predicate rather than a filter over the
#: result for the reason every read in this codebase is: a post-filter is one
#: `continue` away from being skipped, and this one is the difference between
#: a reminder and telling somebody what was said in a room they were removed
#: from.
#:
#: Everything else that can change between the claim and the send is re-read
#: here too, for the same reason: the ask, so an obligation answered in that
#: window is not mentioned, and the person's own switch, so somebody who says
#: stop while the pass is walking its batch is not messaged.
PENDING_FOR_VIEWER = text("""
SELECT n.ask_key, n.channel_id, n.source_message_id,
       c.name AS channel_name,
       a.kind, a.text, a.asked_at,
       rq.display_name AS requester_display,
       m.content AS source_content
FROM notification n
JOIN ask a ON a.ask_key = n.ask_key
JOIN message m ON m.id = n.source_message_id AND m.deleted_at IS NULL
JOIN channel c ON c.id = n.channel_id
JOIN person rq ON rq.id = a.requester_person_id
WHERE n.person_id = :person_id
  AND n.settled_at IS NULL
  AND n.channel_id = ANY(:channel_ids)
  AND a.status IN ('open', 'stale')
  AND NOT EXISTS (SELECT 1 FROM ask_correction c2 WHERE c2.ask_key = n.ask_key)
  -- The person's own switch, re-read here and not only at the claim. A pass
  -- claims up to `recipients_per_pass` identities in one query and then walks
  -- them one at a time, each costing an ACL resolve, a user lookup and a send,
  -- so somebody at the back of a batch is claimed seconds before they are
  -- messaged. Somebody who types `/notifications off` in that gap gets the
  -- confirmation and then, without this, the message anyway. As a predicate on
  -- the last read before the send, "off" means the batch comes back empty and
  -- nothing is sent; their rows stay pending, because turning them off stops
  -- the message rather than discarding what is owed.
  AND NOT EXISTS (
      SELECT 1 FROM notification_preference pref
      WHERE pref.person_id = n.person_id
        AND (NOT pref.enabled OR pref.undeliverable_at IS NOT NULL)
  )
  -- And withdrawal from the corpus, for the same reason and one worse: the
  -- opt-out trigger deletes these rows, so this predicate is usually
  -- redundant -- but it is the bound this file claims, and a bound that holds
  -- only because a different feature's trigger fires is not a bound.
  AND NOT EXISTS (
      SELECT 1 FROM person_opt_out o WHERE o.person_id = n.person_id
  )
ORDER BY n.queued_at
LIMIT :limit
""")

#: The complement of the read above, under the same bound set.
SETTLE_UNREADABLE = text("""
UPDATE notification SET settled_at = :now, outcome = 'unreadable'
WHERE person_id = :person_id
  AND settled_at IS NULL
  AND channel_id <> ALL(:channel_ids)
""")

MARK_SENT = text("""
UPDATE notification SET settled_at = :now, outcome = 'sent'
WHERE person_id = :person_id
  AND settled_at IS NULL
  AND ask_key = ANY(:ask_keys)
""")

#: Delivery both starts the rate-limit interval and records that this person
#: has now been messaged once, which is what makes "say how to stop" a fact
#: about the first message rather than a guess.
RECORD_SENT = text("""
INSERT INTO notification_preference (person_id, first_notified_at, last_notified_at)
VALUES (:person_id, :now, :now)
ON CONFLICT (person_id) DO UPDATE SET
    first_notified_at = COALESCE(
        notification_preference.first_notified_at, EXCLUDED.first_notified_at
    ),
    last_notified_at = EXCLUDED.last_notified_at,
    updated_at = now()
""")

#: A delivery was attempted and did not succeed. Stamped instead of
#: `last_notified_at`, never as well as it: whether this person has ever been
#: messaged decides whether the next message says how to stop, and a failed
#: attempt is no evidence either way. `first_notified_at` therefore stays NULL
#: and the first message that does land still explains the way out.
RECORD_ATTEMPT = text("""
INSERT INTO notification_preference (person_id, last_attempt_at)
VALUES (:person_id, :now)
ON CONFLICT (person_id) DO UPDATE SET
    last_attempt_at = EXCLUDED.last_attempt_at,
    updated_at = now()
""")

RECORD_UNDELIVERABLE = text("""
INSERT INTO notification_preference (person_id, undeliverable_at)
VALUES (:person_id, :now)
ON CONFLICT (person_id) DO UPDATE SET
    undeliverable_at = EXCLUDED.undeliverable_at,
    updated_at = now()
""")

#: Their queue is emptied with the record, so a closed direct message costs one
#: refused send rather than one per obligation for ever.
SETTLE_UNDELIVERABLE = text("""
UPDATE notification SET settled_at = :now, outcome = 'undeliverable'
WHERE person_id = :person_id AND settled_at IS NULL
""")

# --- the person's own switch -------------------------------------------

#: Turning them back on clears the closed-DM record, deliberately. Somebody
#: who has just typed a command at us is somebody we have a channel to; the
#: mark is an observation, not a punishment.
SET_ENABLED = text("""
INSERT INTO notification_preference (person_id, enabled)
VALUES (:person_id, :enabled)
ON CONFLICT (person_id) DO UPDATE SET
    enabled = EXCLUDED.enabled,
    undeliverable_at = CASE
        WHEN EXCLUDED.enabled THEN NULL
        ELSE notification_preference.undeliverable_at
    END,
    updated_at = now()
RETURNING enabled, undeliverable_at
""")

READ_PREFERENCE = text("""
SELECT enabled, undeliverable_at
FROM notification_preference
WHERE person_id = :person_id
""")

# --- health -------------------------------------------------------------

#: Bounded, like the extraction backlog's count: "1000+" says "still draining"
#: as usefully as the true number, and the true number is a scan.
QUEUE_DEPTH = text("""
SELECT count(*) AS pending
FROM (
    SELECT 1 FROM notification WHERE settled_at IS NULL LIMIT :cap
) AS bounded
""")


def statements() -> Sequence[TextClause]:
    """The statements that read queued content, for the audit next door."""
    return (PENDING_FOR_VIEWER,)
