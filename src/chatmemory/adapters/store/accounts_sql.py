"""SQL for account provisioning records (migration 0035).

These are not channel content, so no statement binds `:channel_ids`; each is
registered in the SQL audit with its reason. Every statement that reads or
changes a person's rows is keyed on that person's id, except the two that
cannot be: redeeming a link code is keyed on the code's hash (the code is the
proof), and the cleanup deletes by age alone.

A reservation takes `LOCK_PERSON` first, a row lock on the person held to the
end of the transaction, so the limit's count and the insert that follows it
run one person at a time, as for feature requests.
"""

from __future__ import annotations

from sqlalchemy import text

LOCK_PERSON = text("""
SELECT id FROM person WHERE id = :person_id FOR NO KEY UPDATE
""")

REQUESTS_SINCE = text("""
SELECT requested_at FROM account_provisioning_request
WHERE person_id = :person_id AND requested_at > CAST(:since AS timestamptz)
ORDER BY requested_at
""")

INSERT_CONSENT = text("""
INSERT INTO account_consent (person_id, consent_version, email_hmac, consented_at)
VALUES (:person_id, :consent_version, :email_hmac, CAST(:now AS timestamptz))
""")

INSERT_REQUEST = text("""
INSERT INTO account_provisioning_request (person_id, email_hmac, requested_at)
VALUES (:person_id, :email_hmac, CAST(:now AS timestamptz))
RETURNING id
""")

DELETE_REQUEST = text("""
DELETE FROM account_provisioning_request WHERE id = :request_id
""")

LATEST_CONSENT = text("""
SELECT email_hmac FROM account_consent
WHERE person_id = :person_id
ORDER BY consented_at DESC, id DESC
LIMIT 1
""")

CODES_SINCE = text("""
SELECT count(*) AS issued, min(created_at) AS oldest FROM account_link_code
WHERE person_id = :person_id AND created_at > CAST(:since AS timestamptz)
""")

#: Superseding moves `expires_at` to the time of issue, so "expired" is the one
#: condition a redemption checks, whatever ended the code.
SUPERSEDE_CODES = text("""
UPDATE account_link_code SET expires_at = CAST(:now AS timestamptz)
WHERE person_id = :person_id
  AND used_at IS NULL
  AND expires_at > CAST(:now AS timestamptz)
""")

INSERT_CODE = text("""
INSERT INTO account_link_code (code_sha256, person_id, email_hmac, created_at, expires_at)
VALUES (:code_sha256, :person_id, :email_hmac, CAST(:now AS timestamptz),
        CAST(:expires_at AS timestamptz))
""")

#: One UPDATE, so two redemptions of one code cannot both succeed.
REDEEM_CODE = text("""
UPDATE account_link_code SET used_at = CAST(:now AS timestamptz)
WHERE code_sha256 = :code_sha256
  AND used_at IS NULL
  AND expires_at > CAST(:now AS timestamptz)
RETURNING person_id, email_hmac
""")

DELETE_OLD_REQUESTS = text("""
DELETE FROM account_provisioning_request WHERE requested_at < CAST(:before AS timestamptz)
""")

DELETE_OLD_CODES = text("""
DELETE FROM account_link_code WHERE created_at < CAST(:before AS timestamptz)
""")

# --- linking (0035, 0036) -----------------------------------------------------
#
# Redeeming a code and making the link is one transaction: the code row is
# locked, compared, and marked used only when the link is written.

LOCK_LIVE_CODE = text("""
SELECT person_id, email_hmac FROM account_link_code
WHERE code_sha256 = :code_sha256
  AND used_at IS NULL
  AND expires_at > CAST(:now AS timestamptz)
FOR UPDATE
""")

USE_CODE = text("""
UPDATE account_link_code SET used_at = CAST(:now AS timestamptz)
WHERE code_sha256 = :code_sha256
""")

SUBJECT_OWNER = text("""
SELECT person_id FROM person_account_link WHERE sub = :sub
""")

LINKED_SUBJECT = text("""
SELECT sub FROM person_account_link WHERE person_id = :person_id FOR UPDATE
""")

#: A relink replaces the person's row, and is announced again.
UPSERT_LINK = text("""
INSERT INTO person_account_link (person_id, issuer, sub, email_hint, linked_at)
VALUES (:person_id, :issuer, :sub, :email_hint, CAST(:now AS timestamptz))
ON CONFLICT (person_id) DO UPDATE
SET issuer = EXCLUDED.issuer,
    sub = EXCLUDED.sub,
    email_hint = EXCLUDED.email_hint,
    linked_at = EXCLUDED.linked_at,
    notified_at = NULL
""")

END_USER_SESSIONS = text("""
DELETE FROM user_session WHERE sub = :sub
""")

#: The person's Discord identity, which every other store is keyed on.
LINKED_PERSON = text("""
SELECT p.platform, p.platform_user_id
FROM person_account_link l
JOIN person_platform_id p ON p.person_id = l.person_id
WHERE l.sub = :sub
ORDER BY p.platform = 'discord' DESC, p.platform_user_id
LIMIT 1
""")

DELETE_LINK = text("""
DELETE FROM person_account_link WHERE person_id = :person_id RETURNING sub
""")

UNANNOUNCED_LINKS = text("""
SELECT DISTINCT ON (l.linked_at, l.person_id)
       l.person_id, l.sub, l.email_hint, p.platform, p.platform_user_id
FROM person_account_link l
JOIN person_platform_id p ON p.person_id = l.person_id
WHERE l.notified_at IS NULL
ORDER BY l.linked_at, l.person_id, p.platform = 'discord' DESC, p.platform_user_id
LIMIT :limit
""")

MARK_ANNOUNCED = text("""
UPDATE person_account_link SET notified_at = CAST(:now AS timestamptz)
WHERE sub = :sub AND notified_at IS NULL
""")
