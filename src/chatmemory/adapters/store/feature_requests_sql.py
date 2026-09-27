"""SQL for feature requests.

Suggestions are not channel content, so no statement here binds
`:channel_ids`; each is registered in the SQL audit with its reason. What
stands in for the channel predicate is the person predicate: every statement
that reads or changes a row is keyed on the requester's own person id, bound in
the WHERE clause, so reading or changing somebody else's suggestion is not a
query this module can run.

A submission first takes `LOCK_PERSON`, a row lock on the requester's person
row held to the end of the transaction. That is what makes the daily limit
hold: under READ COMMITTED the count in `INSERT_WITHIN_LIMIT` sees only
committed rows, so without the lock parallel submissions would each count the
same total and all write. With it they run one after another, and each count
sees the rows the previous one committed. `FOR NO KEY UPDATE` does not block
the key-share locks other tables' foreign keys take on the person row.

A BEFORE INSERT trigger (migration 0030) also drops the row for a person who
has opted out, and RETURNING then yields nothing; the adapter checks the
opt-out itself first, so it writes nothing, not even their name.
"""

from __future__ import annotations

from sqlalchemy import text

#: Only a placeholder name is replaced -- the account id a person row is
#: created with when somebody talks to the assistant before ingest has seen
#: them. A name ingest already keeps current is left alone.
LOCK_PERSON = text("""
SELECT id FROM person WHERE id = :person_id FOR NO KEY UPDATE
""")

NAME_PLACEHOLDER_PERSON = text("""
UPDATE person SET display_name = :display_name
WHERE id = :person_id AND display_name = :placeholder
""")

EXISTING_BY_HASH = text("""
SELECT id FROM feature_request
WHERE person_id = :person_id AND normalized_hash = :normalized_hash
""")

INSERT_WITHIN_LIMIT = text("""
INSERT INTO feature_request (
    person_id, text, normalized_hash, language, source_kind, platform,
    guild_id, channel_id, created_at, updated_at
)
SELECT :person_id, :text, :normalized_hash, :language, :source_kind, :platform,
       :guild_id, :channel_id, CAST(:now AS timestamptz), CAST(:now AS timestamptz)
WHERE (
    SELECT count(*) FROM feature_request
    WHERE person_id = :person_id
      AND created_at > CAST(:now AS timestamptz) - interval '24 hours'
) < :daily_limit
ON CONFLICT (person_id, normalized_hash) DO NOTHING
RETURNING id
""")

IS_OPTED_OUT = text("""
SELECT EXISTS (SELECT 1 FROM person_opt_out WHERE person_id = :person_id)
""")

FOR_PERSON = text("""
SELECT id, text, status, created_at, notify_on_change
FROM feature_request
WHERE person_id = :person_id
ORDER BY created_at DESC, id DESC
LIMIT :limit
""")

#: Bound by person as well as by id, so the number in somebody's listing
#: cannot be used to change another person's row. `notified_status` starts at
#: the status they were told about in the acknowledgement, so the status sweep
#: messages them about the next change and not about this one.
SET_NOTIFY = text("""
UPDATE feature_request
   SET notify_on_change = :notify,
       notified_status = COALESCE(notified_status, status)
WHERE id = :request_id AND person_id = :person_id
""")

# --- triage: the console's view of every suggestion ---------------------------
#
# These read every person's suggestions, which is the point: the console is
# where the team reads them. A suggestion is the person's own words, given to
# the team with a disclosure, and never channel content, so there is no
# channel predicate to bind. What is returned is the row itself, the person's
# current display name, and a count -- never the platform id.

_TRIAGE_COLUMNS = """
    fr.id, fr.text, fr.language, fr.status, fr.admin_note, fr.duplicate_of,
    fr.source_kind, fr.created_at, fr.updated_at, fr.updated_by,
    p.display_name AS person_name,
    (SELECT count(DISTINCT other.person_id) FROM feature_request other
      WHERE other.normalized_hash = fr.normalized_hash
        AND other.person_id <> fr.person_id) AS same_text_elsewhere
"""

TRIAGE_PAGE = text(f"""
SELECT {_TRIAGE_COLUMNS}
FROM feature_request fr
JOIN person p ON p.id = fr.person_id
WHERE (CAST(:status AS text) IS NULL OR fr.status = CAST(:status AS text))
ORDER BY fr.created_at DESC, fr.id DESC
LIMIT :limit OFFSET :offset
""")

TRIAGE_COUNT = text("""
SELECT count(*) FROM feature_request
WHERE (CAST(:status AS text) IS NULL OR status = CAST(:status AS text))
""")

#: Locked for the rest of the transaction, so the "before" an admin's change
#: is recorded against is the row the change was applied to.
TRIAGE_ROW = text(f"""
SELECT {_TRIAGE_COLUMNS}
FROM feature_request fr
JOIN person p ON p.id = fr.person_id
WHERE fr.id = :request_id
FOR UPDATE OF fr
""")

TRIAGE_EXISTS = text("""
SELECT EXISTS (SELECT 1 FROM feature_request WHERE id = :request_id)
""")

TRIAGE_UPDATE = text("""
UPDATE feature_request
   SET status = :status,
       admin_note = :admin_note,
       duplicate_of = :duplicate_of,
       updated_at = CAST(:now AS timestamptz),
       updated_by = :updated_by
WHERE id = :request_id
""")

# --- status news: the bot's sweep ------------------------------------------------
#
# A row is news when its author said yes to "tell you when its status
# changes?" (`notify_on_change`, which also set `notified_status` to the
# status they were told about) and the status has moved since. Claiming
# advances `notified_status` first, so each change is sent once; an unsent
# message is put back with RELEASE_STATUS_NEWS.
#
# Skipped, and left unclaimed: people who opted out (their rows are purged
# anyway), people who turned notifications off, and people whose direct
# messages are known to be closed. The recipient is the platform account the
# suggestion was made from.

CLAIM_STATUS_NEWS = text("""
WITH due AS (
    SELECT fr.id, fr.notified_status AS previous,
           ppi.platform, ppi.platform_user_id
    FROM feature_request fr
    JOIN LATERAL (
        SELECT platform, platform_user_id FROM person_platform_id
        WHERE person_id = fr.person_id AND platform = fr.platform
        ORDER BY platform_user_id
        LIMIT 1
    ) ppi ON true
    LEFT JOIN notification_preference pref ON pref.person_id = fr.person_id
    WHERE fr.notify_on_change
      AND fr.notified_status IS NOT NULL
      AND fr.status <> fr.notified_status
      AND NOT EXISTS (
          SELECT 1 FROM person_opt_out o WHERE o.person_id = fr.person_id
      )
      AND (pref.person_id IS NULL
           OR (pref.enabled AND pref.undeliverable_at IS NULL))
    ORDER BY fr.updated_at, fr.id
    LIMIT :limit
    FOR UPDATE OF fr SKIP LOCKED
)
UPDATE feature_request fr
   SET notified_status = fr.status
  FROM due
 WHERE fr.id = due.id
RETURNING fr.id, fr.status, fr.language, fr.text, due.previous,
          due.platform, due.platform_user_id
""")

RELEASE_STATUS_NEWS = text("""
UPDATE feature_request
   SET notified_status = :previous
WHERE id = :request_id AND notified_status = :claimed
""")
