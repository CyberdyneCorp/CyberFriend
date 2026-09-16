"""SQL for conversation memory, and where its permission predicate lives.

Memory follows the rule every other viewer-scoped read here follows: what a
person may see is decided *inside* the statement that returns content, with
their current readable set bound as `:channel_ids`. It is never a filter over
rows already fetched -- a post-filter is one forgotten call away from being no
filter, and a LIMIT applied before it silently returns fewer turns than exist.

The predicate is array containment: a turn's `channel_ids` must be contained in
the viewer's readable set, so a single revoked channel withholds the turn. A
summary's `covered_channel_ids` is the union of what it replaced and is checked
the same way, so it is withheld whole.

Whose conversation is read is bound from the viewer's own identity, through
`person_platform_id`, in the same WHERE clause. There is no statement here that
returns a turn for a person id supplied on its own.

Memory rows are hard-deleted -- by /forget, retention, opt-out and person
deletion -- and are never tombstoned, which is why the reads below carry no
`deleted_at` predicate (registered in the SQL audit).
"""

from __future__ import annotations

from sqlalchemy import text

# Shared fragments. Composed into complete statements at import time, so the
# audit sees whole statements and nothing is assembled per request.
_VIEWER_PERSON = """
person_id = (
    SELECT person_id FROM person_platform_id
    WHERE platform = :platform AND platform_user_id = :platform_user_id
)
"""

_LOCATION = """
location_direct = :location_direct
AND location_platform = :location_platform
AND location_id = :location_id
"""

# --- writes ------------------------------------------------------------

# RETURNING id so the adapter can tell "stored" from "dropped by the opt-out
# trigger": a trigger returning NULL skips the row, and RETURNING then yields
# nothing.
INSERT_TURN = text("""
INSERT INTO conversation_turn (
    person_id, location_platform, location_id, location_direct,
    question, answer, channel_ids
) VALUES (
    :person_id, :location_platform, :location_id, :location_direct,
    :question, :answer, CAST(:source_channel_ids AS bigint[])
)
RETURNING id
""")

# Serialises summarisation per person. Two summarisers racing would each
# delete what the other is about to summarise, and the later write would
# replace a newer summary with an older one. Per person rather than per
# conversation: coarser, but one person's summaries are rare and cheap, and a
# key derived by hashing the location would be one more thing to get wrong.
# The id space is shared with `sql.LOCK_MESSAGE`; a collision only makes one
# transaction wait for another, never lets two run together.
LOCK_PERSON_MEMORY = text("""
SELECT pg_advisory_xact_lock(CAST(:person_id AS bigint))
""")

# One statement: delete the turns and summaries being replaced and insert the
# summary whose covered channels are the union of theirs. The union is computed
# here from the deleted rows, never accepted from the caller -- a summariser
# that under-reported its provenance would otherwise launder a revoked
# channel's content past the recall check.
#
# `guard` refuses a stale write: if a summary already covers this turn or a
# later one, a slower summariser must not replace it with less. `HAVING` makes
# the insert a no-op when nothing was replaced, so an empty summary with no
# provenance (which would pass every check) is never written.
REPLACE_WITH_SUMMARY = text(f"""
WITH guard AS (
    SELECT NOT EXISTS (
        SELECT 1 FROM conversation_summary
        WHERE person_id = :person_id AND {_LOCATION}
          AND through_turn_id >= :through_turn_id
    ) AS ok
),
replaced_turns AS (
    DELETE FROM conversation_turn
    WHERE person_id = :person_id AND {_LOCATION}
      AND id <= :through_turn_id
      AND (SELECT ok FROM guard)
    RETURNING channel_ids AS ids, created_at AS at
),
replaced_summaries AS (
    DELETE FROM conversation_summary
    WHERE person_id = :person_id AND {_LOCATION}
      AND (SELECT ok FROM guard)
    RETURNING covered_channel_ids AS ids, starts_at AS at
),
provenance AS (
    SELECT ids, at FROM replaced_turns
    UNION ALL
    SELECT ids, at FROM replaced_summaries
)
INSERT INTO conversation_summary (
    person_id, location_platform, location_id, location_direct,
    text, covered_channel_ids, through_turn_id, starts_at
)
SELECT
    CAST(:person_id AS bigint), CAST(:location_platform AS text),
    CAST(:location_id AS bigint), CAST(:location_direct AS boolean),
    CAST(:text AS text),
    COALESCE(
        (SELECT array_agg(DISTINCT c ORDER BY c) FROM provenance, unnest(ids) AS c),
        CAST('{{}}' AS bigint[])
    ),
    CAST(:through_turn_id AS bigint),
    MIN(at)
FROM provenance
HAVING COUNT(*) > 0
RETURNING id
""")

FORGET_TURNS_AT = text(f"""
DELETE FROM conversation_turn WHERE {_VIEWER_PERSON} AND {_LOCATION}
""")

FORGET_SUMMARIES_AT = text(f"""
DELETE FROM conversation_summary WHERE {_VIEWER_PERSON} AND {_LOCATION}
""")

FORGET_TURNS_EVERYWHERE = text(f"""
DELETE FROM conversation_turn WHERE {_VIEWER_PERSON}
""")

FORGET_SUMMARIES_EVERYWHERE = text(f"""
DELETE FROM conversation_summary WHERE {_VIEWER_PERSON}
""")

PURGE_TURNS_BEFORE = text("""
DELETE FROM conversation_turn WHERE created_at < CAST(:cutoff AS timestamptz)
""")

# Keyed on the earliest content a summary carries, not on when it was written.
PURGE_SUMMARIES_BEFORE = text("""
DELETE FROM conversation_summary WHERE starts_at < CAST(:cutoff AS timestamptz)
""")

# --- reads: viewer-scoped ----------------------------------------------
#
# The permission predicate, the person predicate and the location predicate are
# all in the WHERE clause ahead of the ranking. The inner query takes the newest
# `:turn_limit` *permitted* turns; the outer one puts them oldest-first, which
# is the order a conversation is read in.
#
# The channel ids come back with the text so a turn answered with this memory in
# its prompt can inherit them (see `ports.memory`).
#
# The opt-out check is belt and braces: the purge trigger in 0013 has already
# removed an opted-out person's rows, and this makes a purge that somehow did
# not run still unable to replay them.

RECALL_TURNS = text(f"""
SELECT id, question, answer, created_at, channel_ids FROM (
    SELECT t.id, t.question, t.answer, t.created_at, t.channel_ids
    FROM conversation_turn t
    WHERE t.channel_ids <@ CAST(:channel_ids AS bigint[])
      AND t.{_VIEWER_PERSON.strip()}
      AND {_LOCATION}
      AND NOT EXISTS (SELECT 1 FROM person_opt_out o WHERE o.person_id = t.person_id)
    ORDER BY t.id DESC
    LIMIT :turn_limit
) permitted
ORDER BY id ASC
""")

RECALL_SUMMARIES = text(f"""
SELECT s.text, s.through_turn_id, s.created_at, s.covered_channel_ids
FROM conversation_summary s
WHERE s.covered_channel_ids <@ CAST(:channel_ids AS bigint[])
  AND s.{_VIEWER_PERSON.strip()}
  AND {_LOCATION}
  AND NOT EXISTS (SELECT 1 FROM person_opt_out o WHERE o.person_id = s.person_id)
ORDER BY s.through_turn_id ASC
""")
