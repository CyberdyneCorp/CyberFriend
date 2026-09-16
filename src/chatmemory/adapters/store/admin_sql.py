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
    (operator, setting, kind, before_value, after_value, reason, recorded_at)
VALUES
    (:operator, :setting, :kind, :before_value, :after_value, :reason,
     coalesce(CAST(:recorded_at AS timestamptz), now()))
RETURNING id, recorded_at
""")

RECENT_CONFIG_AUDIT = text("""
SELECT id, recorded_at, operator, setting, kind, before_value, after_value, reason
FROM config_audit
ORDER BY id DESC
LIMIT :limit
""")
