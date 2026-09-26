"""SQL for `/privacy`: everything held about one person, read-only.

Every statement is keyed on the asker's own person id, bound in the WHERE
clause, so nobody else's rows are a query this module can run. The two that
read channel content -- archived messages and their media -- also bind
`:channel_ids`, the channels the person can read now, so a message in a
channel they cannot read is neither counted nor named.
"""

from __future__ import annotations

from sqlalchemy import text

IS_OPTED_OUT = text("""
SELECT EXISTS (SELECT 1 FROM person_opt_out WHERE person_id = :person_id)
""")

PLATFORMS = text("""
SELECT DISTINCT platform FROM person_platform_id
WHERE person_id = :person_id
ORDER BY platform
""")

FACTS = text("""
SELECT kind, value FROM person_fact
WHERE person_id = :person_id
ORDER BY kind, value
""")

MEMORY_COUNTS = text("""
SELECT location_direct AS direct, sum(turns) AS turns, sum(summaries) AS summaries
FROM (
    SELECT location_direct, 1 AS turns, 0 AS summaries
    FROM conversation_turn WHERE person_id = :person_id
    UNION ALL
    SELECT location_direct, 0, 1
    FROM conversation_summary WHERE person_id = :person_id
) held
GROUP BY location_direct
""")

RECENT_QUESTIONS = text("""
SELECT question FROM conversation_turn
WHERE person_id = :person_id
ORDER BY created_at DESC, id DESC
LIMIT :limit
""")

TASKS = text("""
SELECT id, question, interval_hours, last_outcome, disabled_at IS NOT NULL AS disabled
FROM scheduled_task
WHERE person_id = :person_id
ORDER BY id
""")

ALERTS = text("""
SELECT id, kind, chain, address, asset FROM position_alert
WHERE person_id = :person_id
ORDER BY id
""")

#: No preference row means the person never chose; the setting reads as NULL.
NOTIFICATIONS = text("""
SELECT
    (SELECT enabled FROM notification_preference WHERE person_id = :person_id) AS enabled,
    (SELECT count(*) FROM notification
      WHERE person_id = :person_id AND settled_at IS NULL) AS queued
""")

VOICE_SECONDS = text("""
SELECT COALESCE(sum(seconds), 0) FROM media_usage
WHERE person_id = :person_id AND month = :month
""")

MEDIA = text("""
SELECT mm.kind, count(*) AS rows, count(mm.text) AS with_text
FROM message_media mm
JOIN message m ON m.id = mm.message_id
WHERE m.author_person_id = :person_id
  AND m.channel_id = ANY(:channel_ids)
  AND m.deleted_at IS NULL
GROUP BY mm.kind
ORDER BY mm.kind
""")

SUGGESTIONS = text("""
SELECT id, text, status FROM feature_request
WHERE person_id = :person_id
ORDER BY id DESC
""")

#: Only live tokens: a revoked one no longer grants anything and holds only
#: its label and a hash.
TOKENS = text("""
SELECT t.label, t.issued_at
FROM mcp_token t
JOIN person_platform_id p
  ON p.platform = t.platform AND p.platform_user_id = t.platform_user_id
WHERE p.person_id = :person_id AND t.revoked_at IS NULL
ORDER BY t.issued_at
""")

ARCHIVED_BY_CHANNEL = text("""
SELECT channel_id, count(*) AS messages
FROM message
WHERE author_person_id = :person_id
  AND channel_id = ANY(:channel_ids)
  AND deleted_at IS NULL
GROUP BY channel_id
ORDER BY channel_id
""")

#: Still held in the trace store: a trace whose deletion was requested but
#: not yet confirmed is still there, so it is counted.
TRACES = text("""
SELECT count(*) FROM trace_export t
JOIN person_platform_id p ON p.platform_user_id = t.asker_platform_user_id
WHERE p.person_id = :person_id AND t.deleted_at IS NULL
""")
