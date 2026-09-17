"""SQL for personal facts.

Facts are not channel content, so no statement here binds `:channel_ids`; each
is registered in the SQL audit with its reason. What stands in for the channel
predicate is the person predicate: every statement that reads or deletes is
keyed on the requester's own platform identity, bound in the WHERE clause.
There is no statement that returns a fact for a person id supplied on its own,
so reading another member's email is not a query this module can run.

Facts are hard-deleted -- by the person, `/forget` everywhere, opt-out and
person deletion -- and never tombstoned.
"""

from __future__ import annotations

from sqlalchemy import text

_REQUESTER = """
person_id = (
    SELECT person_id FROM person_platform_id
    WHERE platform = :platform AND platform_user_id = :platform_user_id
)
"""

# One row per (person, kind); setting again replaces. RETURNING tells "stored"
# from "dropped by the opt-out trigger": a BEFORE INSERT trigger returning NULL
# skips the row before conflict handling, and RETURNING then yields nothing.
UPSERT_FACT = text("""
INSERT INTO person_fact (person_id, kind, value)
VALUES (:person_id, :kind, :value)
ON CONFLICT (person_id, kind)
DO UPDATE SET value = EXCLUDED.value, updated_at = now()
RETURNING id
""")

# The opt-out check is belt and braces, as in memory_sql: the purge trigger in
# 0014 has already removed the rows.
FACTS_OF_REQUESTER = text(f"""
SELECT f.kind, f.value, f.updated_at
FROM person_fact f
WHERE f.{_REQUESTER.strip()}
  AND NOT EXISTS (SELECT 1 FROM person_opt_out o WHERE o.person_id = f.person_id)
ORDER BY f.kind
""")

FORGET_FACT = text(f"""
DELETE FROM person_fact WHERE {_REQUESTER} AND kind = :kind
""")

FORGET_ALL_FACTS = text(f"""
DELETE FROM person_fact WHERE {_REQUESTER}
""")
