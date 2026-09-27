"""SQL for self-service erasure: the durable request and its plain-SQL steps.

Every statement is keyed on one person id or one request id, never on a
channel, and none returns message text: the request row holds counts only.
Each write is idempotent, because a resumed erasure repeats the step it
stopped in.
"""

from __future__ import annotations

from sqlalchemy import text

#: One open request per person (`ux_erasure_request_open`). Asking again
#: returns the open one; asking to also stop archiving upgrades it and sends it
#: back to step 1, so the opt-out step runs. Every step is idempotent, so
#: repeating the ones already done costs nothing.
OPEN_REQUEST = text("""
INSERT INTO erasure_request (person_id, mode, counts)
VALUES (:person_id, :mode, CAST(:counts AS jsonb))
ON CONFLICT (person_id) WHERE completed_at IS NULL DO UPDATE
SET mode = CASE WHEN EXCLUDED.mode = 'erase_and_opt_out' THEN EXCLUDED.mode
                ELSE erasure_request.mode END,
    step = CASE WHEN EXCLUDED.mode = 'erase_and_opt_out'
                     AND erasure_request.mode <> 'erase_and_opt_out'
                THEN 1 ELSE erasure_request.step END,
    updated_at = now()
RETURNING id, person_id, mode, step, requested_at, counts
""")

#: The request time, not now: a repeated step must not move the cut, and a
#: message created after the request is a new message.
STOP_REIMPORT = text("""
UPDATE person
SET erased_before = GREATEST(COALESCE(erased_before, :requested_at), :requested_at)
WHERE id = :person_id
""")

RECORD_TRACES = text("""
UPDATE erasure_request
SET counts = counts || jsonb_build_object('traces', CAST(:traces AS integer))
WHERE id = :id
""")

PURGE_DERIVED = text("SELECT purge_person_derived(:person_id)")

#: The voice ledger's own lock (`media_sql.LOCK_LEDGER`), so a fold cannot
#: interleave with a charge's check, or with a second fold of the same rows.
FOLD_VOICE = text("""
WITH moved AS (
    DELETE FROM media_usage WHERE person_id = :person_id
    RETURNING month, purpose, seconds
), summed AS (
    SELECT month, purpose, sum(seconds)::integer AS seconds
    FROM moved GROUP BY month, purpose
), folded AS (
    INSERT INTO media_usage_anonymous (month, purpose, seconds)
    SELECT month, purpose, seconds FROM summed
    ON CONFLICT (month, purpose) DO UPDATE
    SET seconds = media_usage_anonymous.seconds + EXCLUDED.seconds, updated_at = now()
    RETURNING 1
)
SELECT COALESCE(sum(seconds), 0) FROM summed
""")

#: A name nobody could have: the unnamed-people repair looks for rows still
#: called by their account id, and this is not one, so it is never renamed.
TOMBSTONE_PERSON = text("""
UPDATE person SET display_name = :placeholder WHERE id = :person_id
""")

RESET_PREFERENCES = text("""
DELETE FROM notification_preference WHERE person_id = :person_id
""")

ADVANCE = text("""
UPDATE erasure_request
SET step = GREATEST(step, CAST(:step AS smallint)),
    updated_at = now(),
    completed_at = CASE WHEN CAST(:step AS smallint) = 8 THEN now() END
WHERE id = :id
RETURNING id, person_id, mode, step, requested_at, counts
""")

OPEN_REQUESTS = text("""
SELECT r.id, r.person_id, r.mode, r.step, r.requested_at, r.counts,
       p.platform, p.platform_user_id
FROM erasure_request r
JOIN LATERAL (
    SELECT platform, platform_user_id FROM person_platform_id
    WHERE person_id = r.person_id
    ORDER BY platform, platform_user_id
    LIMIT 1
) p ON TRUE
WHERE r.completed_at IS NULL AND r.updated_at < :idle_since
ORDER BY r.requested_at, r.id
LIMIT :limit
""")
