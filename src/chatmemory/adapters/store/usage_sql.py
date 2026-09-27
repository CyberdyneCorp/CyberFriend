"""SQL for the usage view: who must not appear, names, and voice seconds.

None of these returns channel content, so none binds `:channel_ids`; each is
registered in the SQL audit with its reason. They run on every usage request,
never cached: the answer to "who has opted out or erased" has to be the
current one.

Platform ids are compared as text because that is how the trace store holds
them (`userId`). A person with an erasure still open is excluded entirely,
and a person whose erasure completed keeps `person.erased_before`, which cuts
everything up to it.
"""

from __future__ import annotations

from sqlalchemy import text

EXCLUDED_PEOPLE = text("""
SELECT DISTINCT p.platform_user_id::text AS user_id
FROM person_platform_id p
WHERE EXISTS (SELECT 1 FROM person_opt_out o WHERE o.person_id = p.person_id)
   OR EXISTS (
        SELECT 1 FROM erasure_request e
        WHERE e.person_id = p.person_id AND e.completed_at IS NULL
   )
""")

ERASED_PEOPLE = text("""
SELECT p.platform_user_id::text AS user_id, pe.erased_before
FROM person_platform_id p
JOIN person pe ON pe.id = p.person_id
WHERE pe.erased_before IS NOT NULL
""")

#: Traces whose deletion was requested and that the trace store may still
#: hold: not confirmed yet, or confirmed within the last day (Langfuse deletes
#: asynchronously). Bounded by the window, with a day's slack either side
#: because the export row is written just after the trace's own timestamp.
PENDING_TRACES = text("""
SELECT trace_id
FROM trace_export
WHERE deletion_requested_at IS NOT NULL
  AND created_at >= :since
  AND created_at < :until
  AND (deleted_at IS NULL OR deleted_at > :confirmed_after)
""")

NAMES = text("""
SELECT p.platform_user_id::text AS user_id, pe.display_name
FROM person_platform_id p
JOIN person pe ON pe.id = p.person_id
WHERE p.platform_user_id = ANY(:ids)
  AND pe.display_name <> ''
""")

PERSON = text("""
SELECT pe.id, pe.display_name, pe.tracing_notice_at
FROM person_platform_id p
JOIN person pe ON pe.id = p.person_id
WHERE p.platform_user_id = :user_id
ORDER BY (p.platform = 'discord') DESC, p.platform
LIMIT 1
""")

#: Seconds per person in the months the window touches, named by the person's
#: lowest platform id; nobody opted out or being erased.
VOICE_BY_PERSON = text("""
SELECT (
         SELECT min(pp.platform_user_id) FROM person_platform_id pp
         WHERE pp.person_id = m.person_id
       )::text AS user_id,
       sum(m.seconds)::bigint AS seconds
FROM media_usage m
WHERE m.month >= :first_month AND m.month < :until
  AND NOT EXISTS (SELECT 1 FROM person_opt_out o WHERE o.person_id = m.person_id)
  AND NOT EXISTS (
        SELECT 1 FROM erasure_request e
        WHERE e.person_id = m.person_id AND e.completed_at IS NULL
  )
GROUP BY m.person_id
""")

VOICE_ANONYMOUS = text("""
SELECT COALESCE(sum(seconds), 0)::bigint
FROM media_usage_anonymous
WHERE month >= :first_month AND month < :until
""")
