"""SQL for position alerts. The shape is `schedules_sql`'s, for the same reasons.

`CLAIM_DUE` selects, advances and returns in one statement, so an alert is
read once per sweep however many sweeps run, and it advances to `now() +
sweep` rather than from its old time, so an outage is one check on return and
not a burst of them. Unlike a scheduled task it returns the whole row: the
evaluation needs the stored state, and reading it in the same statement is
what makes the claim and the state it acts on the same snapshot.

`CREATE_WITHIN_CAP` makes the per-person cap a predicate on the insert, as for
scheduled tasks. Only active alerts count: one stopped by a closed position is
kept for the listing, and should not use up a slot.
"""

from __future__ import annotations

from sqlalchemy import text

RESOLVE_PERSON_ID = text("""
SELECT person_id FROM person_platform_id
WHERE platform = :platform AND platform_user_id = :platform_user_id
""")

_COLUMNS = """
id, person_id, kind, chain, address, address_source, protocol, token_id, pool_ref,
token0_symbol, token1_symbol, token0_decimals, token1_decimals, fee, threshold,
notify_return, language, state, state_since, pending_state, pending_count,
last_value, consecutive_failures, created_at, next_check_at, last_checked_at,
last_fired_at, disabled_at, disabled_reason
"""

#: Insert only while the person is under their cap, and not twice.
#:
#: The conflict target names `ux_position_alert_target` by its expressions and
#: predicate, so a duplicate is skipped rather than raised; the caller tells a
#: skip from the cap by counting afterwards.
CREATE_WITHIN_CAP = text(f"""
INSERT INTO position_alert (
    person_id, kind, chain, address, address_source, protocol, token_id, pool_ref,
    token0_symbol, token1_symbol, token0_decimals, token1_decimals, fee, threshold,
    notify_return, language, state, last_value, next_check_at
)
SELECT :person_id, :kind, :chain, :address, :address_source, :protocol,
       CAST(:token_id AS numeric), :pool_ref, :token0_symbol, :token1_symbol,
       CAST(:token0_decimals AS smallint), CAST(:token1_decimals AS smallint),
       CAST(:fee AS integer), CAST(:threshold AS numeric),
       CAST(:notify_return AS boolean), :language, :state,
       CAST(:last_value AS numeric), CAST(:next_check_at AS timestamptz)
WHERE (
    SELECT count(*) FROM position_alert
     WHERE person_id = :person_id AND disabled_at IS NULL
) < :cap
ON CONFLICT (person_id, kind, chain, address, (coalesce(token_id, -1)),
             (coalesce(threshold, 0)))
   WHERE disabled_at IS NULL
DO NOTHING
RETURNING {_COLUMNS}
""")

ACTIVE_COUNT = text("""
SELECT count(*) FROM position_alert WHERE person_id = :person_id AND disabled_at IS NULL
""")

FOR_PERSON = text(f"""
SELECT {_COLUMNS}
FROM position_alert
WHERE person_id = :person_id
ORDER BY created_at DESC, id DESC
""")

#: Bound by person as well as by id, as `schedules_sql.DELETE_OWN` is.
DELETE_OWN = text("""
DELETE FROM position_alert WHERE id = :alert_id AND person_id = :person_id
""")

#: See the module docstring. `FOR UPDATE SKIP LOCKED` keeps two sweeps from
#: claiming the same row, so correctness does not rest on one replica.
CLAIM_DUE = text(f"""
UPDATE position_alert a
   SET next_check_at = CAST(:now AS timestamptz) + make_interval(secs => :interval)
  FROM (
      SELECT id FROM position_alert
      WHERE disabled_at IS NULL AND next_check_at <= CAST(:now AS timestamptz)
      ORDER BY next_check_at
      LIMIT :limit
      FOR UPDATE SKIP LOCKED
  ) due
 WHERE a.id = due.id
RETURNING {", ".join(f"a.{c.strip()}" for c in _COLUMNS.split(","))},
          (SELECT platform_user_id FROM person_platform_id p
            WHERE p.person_id = a.person_id AND p.platform = :platform
            LIMIT 1) AS platform_user_id
""")

#: One sweep's decision about one alert. A failed read only counts; every
#: other field is what the evaluator returned, which for a failure is the
#: state it already had.
RECORD = text("""
UPDATE position_alert
   SET state = :state,
       state_since = CAST(:state_since AS timestamptz),
       pending_state = :pending_state,
       pending_count = :pending_count,
       last_value = CAST(:last_value AS numeric),
       last_checked_at = CAST(:now AS timestamptz),
       consecutive_failures = CASE WHEN :failed THEN consecutive_failures + 1 ELSE 0 END,
       last_fired_at = CASE WHEN :fired THEN CAST(:now AS timestamptz) ELSE last_fired_at END,
       disabled_at = CASE WHEN CAST(:disable_reason AS text) IS NULL THEN disabled_at
                          ELSE CAST(:now AS timestamptz) END,
       disabled_reason = coalesce(CAST(:disable_reason AS text), disabled_reason)
 WHERE id = :alert_id
""")

#: Stops every alert of a person whose direct messages are closed. Kept rather
#: than deleted, like a disabled scheduled task.
DISABLE_FOR_PERSON = text("""
UPDATE position_alert
   SET disabled_at = CAST(:now AS timestamptz), disabled_reason = :reason
 WHERE person_id = :person_id AND disabled_at IS NULL
""")
