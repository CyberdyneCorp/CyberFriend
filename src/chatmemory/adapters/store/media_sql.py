"""SQL for the media usage ledger.

Accounting, not content: no statement here reads or writes anything a person
said, so none binds `:channel_ids`, and each is registered in the SQL audit
with its reason. Every statement is keyed on one person's own platform identity
or on a month.
"""

from __future__ import annotations

from sqlalchemy import text

# One lock for the whole ledger, held to the end of the transaction: the check
# and the charge below must not interleave with another voice note's, or two of
# them could both fit under the month's last minute.
LOCK_LEDGER = text("SELECT pg_advisory_xact_lock(hashtext('media_usage'))")

PERSON_OF = text("""
SELECT p.person_id,
       EXISTS (SELECT 1 FROM person_opt_out o WHERE o.person_id = p.person_id) AS opted_out
FROM person_platform_id p
WHERE p.platform = :platform AND p.platform_user_id = :platform_user_id
""")

CREATE_PERSON = text("INSERT INTO person (display_name) VALUES (:display_name) RETURNING id")

LINK_PERSON = text("""
INSERT INTO person_platform_id (platform, platform_user_id, person_id)
VALUES (:platform, :platform_user_id, :person_id)
ON CONFLICT DO NOTHING
""")

# What this person has used on their own questions this month, and what
# everybody has used on anything.
MONTH_USAGE = text("""
SELECT
    COALESCE(SUM(seconds) FILTER (
        WHERE person_id = :person_id AND purpose = 'question'
    ), 0) AS mine,
    COALESCE(SUM(seconds), 0) AS everyone
FROM media_usage
WHERE month = :month
""")

CHARGE = text("""
INSERT INTO media_usage (person_id, month, purpose, seconds)
VALUES (:person_id, :month, 'question', :seconds)
ON CONFLICT (person_id, month, purpose)
DO UPDATE SET seconds = media_usage.seconds + EXCLUDED.seconds, updated_at = now()
""")
