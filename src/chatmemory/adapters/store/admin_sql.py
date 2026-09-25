"""SQL for console credentials and the change record.

Two tables, both owned by migration 0012. Nothing here creates schema: a
service that creates its own tables hides an unmigrated database until the
first query against a table it did not think to create, which is the defect
migration 0005 exists to have fixed for `mcp_token`.

No statement in this module binds `:channel_ids`, and that is not an
oversight. The console has no viewer, because it can reach no content: there
is no message, chunk or ask column anywhere below, and adding one would be the
second path into private channels this capability exists to not create. Every
statement is registered with that reason in `tests/unit/test_sql_audit.py`.

What is deliberately absent is as load-bearing as what is present: there is no
UPDATE and no DELETE against `config_audit`. The port cannot express one, this
module does not contain one, and migration 0012 adds a trigger that refuses
one -- three layers, because the first two only bind code that went through
them.
"""

from __future__ import annotations

from sqlalchemy import text

# --- console credentials -----------------------------------------------

# Only the hash is stored, so a database disclosure hands out no working
# credential -- and a leaked backup cannot be replayed against the highest
# privilege surface in the system.
INSERT_ADMIN_TOKEN = text("""
INSERT INTO admin_token (token_hash, operator, label)
VALUES (:token_hash, :operator, :label)
""")

# The only lookup that authenticates. Returns the operator and nothing else:
# there is no column here that a request could have named, so the credential
# is the whole identity.
LOOKUP_ADMIN_TOKEN = text("""
SELECT operator FROM admin_token
WHERE token_hash = :token_hash AND revoked_at IS NULL
""")

# Keyed on one operator, which is what makes revocation routine: withdrawing
# one person's access must never be an outage for everybody else, or it gets
# deferred until after it mattered.
REVOKE_OPERATOR_TOKENS = text("""
UPDATE admin_token SET revoked_at = now()
WHERE operator = :operator AND revoked_at IS NULL
""")

# Lets a grant or a withdrawal be recorded with a before and an after rather
# than only the fact that one happened.
COUNT_LIVE_OPERATOR_TOKENS = text("""
SELECT count(*) FROM admin_token
WHERE operator = :operator AND revoked_at IS NULL
""")

ACTIVE_ADMIN_TOKENS = text("""
SELECT token_hash, operator, label, issued_at
FROM admin_token WHERE revoked_at IS NULL
ORDER BY issued_at, token_hash
""")

# --- the change record -------------------------------------------------

# `recorded_at` is bound rather than always `now()` so a caller can supply a
# deterministic clock in a test; NULL falls back to the server's time, so the
# ordinary path cannot be given a time that suits it.
APPEND_CONFIG_AUDIT = text("""
INSERT INTO config_audit
    (operator, operator_display, setting, kind, before_value, after_value, reason,
     recorded_at)
VALUES
    (:operator, :operator_display, :setting, :kind, :before_value, :after_value, :reason,
     coalesce(CAST(:recorded_at AS timestamptz), now()))
RETURNING id, recorded_at
""")

RECENT_CONFIG_AUDIT = text("""
SELECT id, recorded_at, operator, operator_display, setting, kind, before_value,
       after_value, reason
FROM config_audit
ORDER BY id DESC
LIMIT :limit
""")

# --- CyberdyneAuth sign-in (migration 0031) -----------------------------
#
# Hashes and ciphertext only: the session id, state, nonce and browser binding
# are sha256, the verifier and the tokens AES-GCM under ADMIN_SESSION_KEY. No
# statement here returns content, and none takes a viewer.

INSERT_ADMIN_LOGIN = text("""
INSERT INTO admin_login
    (state_hash, nonce_hash, verifier_enc, binding_hash, purpose, link_code_hash,
     max_age, expires_at)
VALUES
    (:state_hash, :nonce_hash, :verifier_enc, :binding_hash, :purpose, :link_code_hash,
     :max_age, :expires_at)
""")

# Logins live ten minutes; a day of grace keeps a replayed state explainable
# in the table while it is still being looked at.
FORGET_EXPIRED_ADMIN_LOGINS = text("""
DELETE FROM admin_login WHERE expires_at < CAST(:now AS timestamptz) - interval '1 day'
""")

# Used once: marking it used is the lookup, so two callbacks with one state
# cannot both find it.
CONSUME_ADMIN_LOGIN = text("""
UPDATE admin_login SET used_at = :now
WHERE state_hash = :state_hash AND used_at IS NULL AND expires_at > :now
RETURNING state_hash, nonce_hash, verifier_enc, binding_hash, purpose, link_code_hash,
          max_age, expires_at
""")

INSERT_ADMIN_SESSION = text("""
INSERT INTO admin_session
    (id_hash, sub, email, roles, access_token_enc, refresh_token_enc, id_token_enc,
     access_expires_at, created_at, last_seen_at, expires_at)
VALUES
    (:id_hash, :sub, :email, :roles, :access_token_enc, :refresh_token_enc, :id_token_enc,
     :access_expires_at, :created_at, :last_seen_at, :expires_at)
""")

LIVE_ADMIN_SESSION = text("""
SELECT id_hash, sub, email, roles, access_token_enc, refresh_token_enc, id_token_enc,
       access_expires_at, created_at, last_seen_at, expires_at, revoked_at
FROM admin_session
WHERE id_hash = :id_hash AND revoked_at IS NULL AND expires_at > :now
  AND last_seen_at > :idle_since
""")

REFRESH_ADMIN_SESSION = text("""
UPDATE admin_session
SET roles = :roles, access_token_enc = :access_token_enc,
    refresh_token_enc = :refresh_token_enc, id_token_enc = :id_token_enc,
    access_expires_at = :access_expires_at, expires_at = :expires_at, last_seen_at = :now
WHERE id_hash = :id_hash AND revoked_at IS NULL
""")

TOUCH_ADMIN_SESSION = text("""
UPDATE admin_session SET last_seen_at = :now
WHERE id_hash = :id_hash AND revoked_at IS NULL
""")

REVOKE_ADMIN_SESSION = text("""
UPDATE admin_session SET revoked_at = :now
WHERE id_hash = :id_hash AND revoked_at IS NULL
RETURNING id_hash, sub, email, roles, access_token_enc, refresh_token_enc, id_token_enc,
          access_expires_at, created_at, last_seen_at, expires_at, revoked_at
""")
