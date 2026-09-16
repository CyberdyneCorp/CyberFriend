"""SQL for stored configuration.

Nothing here reads message, document or ask content, so nothing here binds a
viewer: these statements carry settings an operator wrote and the record of
who wrote them. Each is registered in the SQL audit with that reason in
writing -- `tests/unit/test_sql_audit.py` finds them by reflection, so adding
a statement to this module without a written reason fails the suite.

Two shapes are deliberate:

*   **A write and its record are one statement.** `config_audit` (migration
    0012) is written by the same statement that changes the setting, reading
    the previous value through a CTE, so there is no window in which a change
    is stored and its record is not. A record written by the caller afterwards
    is a record that a crash, a timeout or an early return can lose -- and a
    change with no record is exactly what environment variables already gave
    us. A console route that edits a setting through this module must
    therefore *not* also append an `applied` entry of its own: the entry is
    already there, and two rows for one edit is a record that cannot be read.

*   **A refusal records the key and the reason, never the value.** The most
    likely refused change is somebody pasting a credential into a settings
    field, and a record that preserved the rejected text would turn that
    mistake into a stored secret -- in the one table built to be kept forever
    and read by operators.
"""

from __future__ import annotations

from sqlalchemy import text

LOAD_SETTINGS = text("""
SELECT key, value, updated_by, updated_at FROM app_setting
""")

# The previous value is read in the same statement that overwrites it, so the
# recorded "before" cannot be a value some concurrent write already replaced:
# every CTE in one statement sees the same snapshot.
UPSERT_SETTING = text("""
WITH previous AS (
    SELECT value FROM app_setting WHERE key = :key
), written AS (
    INSERT INTO app_setting (key, value, updated_by, updated_at)
    VALUES (:key, :value, :operator, :at)
    ON CONFLICT (key) DO UPDATE SET
        value = EXCLUDED.value,
        updated_by = EXCLUDED.updated_by,
        updated_at = EXCLUDED.updated_at
)
INSERT INTO config_audit
    (operator, setting, kind, before_value, after_value, reason, recorded_at)
VALUES (
    :operator, :key, 'applied', (SELECT value FROM previous), :value, NULL, :at
)
""")

# Clearing hands a setting back to the environment, which is an ordinary edit
# with no "after" rather than a removal of history. A key that is not stored
# deletes nothing and records nothing: the INSERT selects from the delete's
# returned rows, so clearing something already cleared is a no-op instead of a
# record of a change that did not happen.
CLEAR_SETTING = text("""
WITH removed AS (
    DELETE FROM app_setting WHERE key = :key RETURNING value
)
INSERT INTO config_audit
    (operator, setting, kind, before_value, after_value, reason, recorded_at)
SELECT :operator, :key, 'applied', removed.value, NULL, :reason, :at FROM removed
""")

# No before and no after, deliberately. See the module docstring: the refused
# value is the one thing that must not be kept.
RECORD_REFUSAL = text("""
INSERT INTO config_audit
    (operator, setting, kind, before_value, after_value, reason, recorded_at)
VALUES (:operator, :key, 'refused', NULL, NULL, :reason, :at)
""")
