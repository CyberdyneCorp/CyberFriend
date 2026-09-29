"""SQL for a person's own connected-app keys (`person_secret`).

Keys are not channel content, so no statement here binds `:channel_ids`; each
is registered in the SQL audit with its reason. What stands in for the channel
predicate is the person predicate: every statement that reads or deletes is
keyed on the requester's own platform identity, bound in the WHERE clause, so
reading somebody else's key is not a query this module can run.

Rows hold ciphertext and the last four characters, never the key. A key saved
by an opted-out person is dropped by the BEFORE INSERT trigger of 0037, and
RETURNING then yields nothing.
"""

from __future__ import annotations

from sqlalchemy import text

_REQUESTER = """
person_id = (
    SELECT person_id FROM person_platform_id
    WHERE platform = :platform AND platform_user_id = :platform_user_id
)
"""

#: One key per (person, kind): connecting again replaces it and its date.
UPSERT_KEY = text("""
INSERT INTO person_secret (person_id, kind, ciphertext, last4)
VALUES (:person_id, :kind, :ciphertext, :last4)
ON CONFLICT (person_id, kind)
DO UPDATE SET ciphertext = EXCLUDED.ciphertext, last4 = EXCLUDED.last4, created_at = now()
RETURNING created_at
""")

#: The opt-out check is belt and braces: the purge has already removed the row.
KEY_OF_REQUESTER = text(f"""
SELECT s.person_id, s.ciphertext
FROM person_secret s
WHERE s.{_REQUESTER.strip()}
  AND s.kind = :kind
  AND NOT EXISTS (SELECT 1 FROM person_opt_out o WHERE o.person_id = s.person_id)
""")

FORGET_ALL_KEYS = text(f"""
DELETE FROM person_secret WHERE {_REQUESTER}
""")

#: What a key may be shown as: its last four characters and date. Never the
#: ciphertext, so showing a key cannot open one.
HELD_KEY_OF_REQUESTER = text(f"""
SELECT s.last4, s.created_at
FROM person_secret s
WHERE s.{_REQUESTER.strip()}
  AND s.kind = :kind
  AND NOT EXISTS (SELECT 1 FROM person_opt_out o WHERE o.person_id = s.person_id)
""")

FORGET_KEY = text(f"""
DELETE FROM person_secret WHERE {_REQUESTER} AND kind = :kind
""")
