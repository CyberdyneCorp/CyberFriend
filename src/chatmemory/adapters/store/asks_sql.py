"""SQL for extracted asks, and the one place their permission predicate lives.

Same rule as the corpus: every statement that returns an ask binds the viewer's
readable channel set into its own WHERE clause, so the filter constrains the
scan rather than its output. It matters more here than for raw messages. An ask
is a *summary* of a conversation -- it travels, it reads as neutral fact, and it
has lost the context that would have signalled sensitivity -- so an ask leaking
out of a channel leaks more than the quote it came from would.

Deleted content stops being reported everywhere: every read joins `message` and
requires the source to be alive, so a tombstone removes the ask from answers,
from counts and from correction the moment it lands.
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

# --- writes ------------------------------------------------------------

UPSERT_ASK = text("""
INSERT INTO ask (
    ask_key, source_message_id, channel_id, thread_id, requester_person_id,
    addressee_kind, addressee_person_id, addressee_group, kind, text,
    confidence, asked_at
) VALUES (
    :ask_key, :source_message_id, :channel_id, CAST(:thread_id AS bigint),
    :requester_person_id, :addressee_kind, CAST(:addressee_person_id AS bigint),
    CAST(:addressee_group AS text), :kind, :text, :confidence, :asked_at
)
ON CONFLICT (ask_key) DO UPDATE SET
    text = EXCLUDED.text,
    confidence = EXCLUDED.confidence,
    channel_id = EXCLUDED.channel_id,
    thread_id = EXCLUDED.thread_id,
    asked_at = EXCLUDED.asked_at
-- status, closed_at and closed_by are deliberately NOT updated. They record
-- events that were observed; a second extraction pass has observed nothing,
-- and resetting an answered ask to open would resurrect finished work.
""")

# Withdraws asks a re-run no longer finds, so reprocessing a window cannot
# leave a stale obligation behind. Corrected asks are exempt: the addressee has
# already spoken about those, and that outranks anything extraction concludes.
PRUNE_ASKS = text("""
DELETE FROM ask a
WHERE a.source_message_id = :source_message_id
  AND a.ask_key <> ALL(CAST(:keep AS text[]))
  AND NOT EXISTS (SELECT 1 FROM ask_correction c WHERE c.ask_key = a.ask_key)
""")

RECORD_REACTION = text("""
INSERT INTO ask_reaction (message_id, person_id, emoji, reacted_at)
SELECT :message_id, :person_id, :emoji, :at
WHERE EXISTS (SELECT 1 FROM message WHERE id = :message_id AND deleted_at IS NULL)
ON CONFLICT (message_id, person_id, emoji) DO NOTHING
""")

# --- state, from observed events only ----------------------------------
#
# No model is consulted by any of these. Each one names an event that either
# happened or did not, which is what makes the state auditable.

CLOSE_ANSWERED_BY_REPLY = text("""
UPDATE ask a SET status = 'answered', closed_at = r.created_at, closed_by = 'reply'
FROM message r
WHERE a.status IN ('open', 'stale')
  AND a.addressee_kind = 'person'
  AND r.author_person_id = a.addressee_person_id
  AND r.deleted_at IS NULL
  AND r.created_at > a.asked_at
  AND r.channel_id = a.channel_id
  -- In-thread, or a direct reply to the asking message. A later message from
  -- the same person elsewhere in the channel is not an answer: people talk
  -- about other things in the same room, and counting that as closure loses
  -- real asks silently.
  AND (
      r.reply_to_id = a.source_message_id
      OR (a.thread_id IS NOT NULL AND r.thread_id = a.thread_id)
  )
""")

CLOSE_ANSWERED_BY_REACTION = text("""
UPDATE ask a SET status = 'answered', closed_at = x.reacted_at, closed_by = 'reaction'
FROM ask_reaction x
WHERE a.status IN ('open', 'stale')
  AND a.addressee_kind = 'person'
  AND x.message_id = a.source_message_id
  AND x.person_id = a.addressee_person_id
  AND x.emoji = ANY(:emojis)
  -- The acknowledgement has to come after the thing it acknowledges. Its
  -- sibling above carries the same guard: without it a tick left on a
  -- message for some earlier reason closes an ask the moment extraction
  -- creates one, so the obligation is answered before anybody has read it.
  AND x.reacted_at > a.asked_at
""")

# Ageing marks an ask stale. It never closes one: an ask nobody answered in
# three weeks is exactly what somebody asking "what do I need to do" wants to
# see, and closing it would hide that.
MARK_STALE = text("""
UPDATE ask SET status = 'stale'
WHERE status = 'open' AND asked_at < CAST(:cutoff AS timestamptz)
""")

# --- reads -------------------------------------------------------------
#
# `:channel_ids` is the viewer's readable set, and `:person_id` is the viewer
# themselves. Neither is ever taken from a caller's request object: asking
# about somebody else's obligations is not expressible through this SQL.

_OBLIGATION_PREDICATE = """
FROM ask a
JOIN message m ON m.id = a.source_message_id AND m.deleted_at IS NULL
JOIN person rq ON rq.id = a.requester_person_id
LEFT JOIN person_platform_id rqp
       ON rqp.person_id = a.requester_person_id AND rqp.platform = :platform
LEFT JOIN person_platform_id adp
       ON adp.person_id = a.addressee_person_id AND adp.platform = :platform
LEFT JOIN ask_correction c ON c.ask_key = a.ask_key
WHERE a.channel_id = ANY(:channel_ids)
  -- Corrections outrank extraction: once the addressee has spoken about an
  -- ask it stops being reported, whether they called it done or not theirs.
  AND c.ask_key IS NULL
  AND a.status = ANY(:statuses)
  AND a.kind = ANY(:kinds)
  AND a.confidence >= :min_confidence
  AND (
      a.addressee_person_id = :person_id
      OR (
          CAST(:include_commitments AS boolean)
          AND a.kind = 'commitment'
          AND a.requester_person_id = :person_id
      )
  )
  AND (CAST(:since AS timestamptz) IS NULL OR a.asked_at >= CAST(:since AS timestamptz))
  AND (CAST(:until AS timestamptz) IS NULL OR a.asked_at <= CAST(:until AS timestamptz))
"""

OBLIGATIONS = text(f"""
SELECT a.id, a.ask_key, a.source_message_id, a.channel_id, a.thread_id,
       a.kind, a.text, a.confidence, a.status, a.asked_at,
       a.addressee_kind, a.addressee_group,
       rq.display_name AS requester_display,
       rqp.platform_user_id AS requester_platform_id,
       adp.platform_user_id AS addressee_platform_id,
       m.content AS source_content
{_OBLIGATION_PREDICATE}
ORDER BY a.asked_at DESC
LIMIT :limit
""")

# Counted under exactly the same predicate as it is reported under. A count
# that included unreadable channels would disclose the existence of a private
# conversation as surely as quoting it would.
COUNT_OBLIGATIONS = text(f"""
SELECT count(*) AS total
{_OBLIGATION_PREDICATE}
""")

ASK_EXISTS = text("""
SELECT a.addressee_person_id
FROM ask a
JOIN message m ON m.id = a.source_message_id AND m.deleted_at IS NULL
WHERE a.ask_key = :ask_key
  AND a.channel_id = ANY(:channel_ids)
""")

# The addressee check is a predicate rather than a step a caller performs, so
# there is no code path in which it can be skipped. Correcting somebody else's
# ask writes nothing and returns nothing.
APPLY_CORRECTION = text("""
INSERT INTO ask_correction (ask_key, by_person_id, resolution)
SELECT a.ask_key, :person_id, :resolution
FROM ask a
JOIN message m ON m.id = a.source_message_id AND m.deleted_at IS NULL
WHERE a.ask_key = :ask_key
  AND a.channel_id = ANY(:channel_ids)
  AND a.addressee_kind = 'person'
  AND a.addressee_person_id = :person_id
ON CONFLICT (ask_key) DO UPDATE SET
    resolution = EXCLUDED.resolution,
    by_person_id = EXCLUDED.by_person_id
RETURNING ask_key
""")


def statements() -> Sequence[TextClause]:
    """Every statement that reads asks, for the test that audits them."""
    return (OBLIGATIONS, COUNT_OBLIGATIONS, ASK_EXISTS, APPLY_CORRECTION)
