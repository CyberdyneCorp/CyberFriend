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
