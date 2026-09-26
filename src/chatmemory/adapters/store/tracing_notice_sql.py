"""SQL for the one-time tracing notice.

Not channel content: the statement reads and writes a notice version and a
time on one person's own row, keyed on their person id, so it binds no
`:channel_ids` and is registered in the SQL audit with its reason.

The claim is one conditional UPDATE, so "shown once" holds without a lock:
under READ COMMITTED a second concurrent UPDATE waits for the first, then
re-checks the version against the committed row and matches nothing.
"""

from __future__ import annotations

from sqlalchemy import text

CLAIM_NOTICE = text("""
UPDATE person
SET tracing_notice_version = :version, tracing_notice_at = :now
WHERE id = :person_id
  AND COALESCE(tracing_notice_version, 0) < :version
  AND NOT EXISTS (SELECT 1 FROM person_opt_out o WHERE o.person_id = :person_id)
RETURNING id
""")
