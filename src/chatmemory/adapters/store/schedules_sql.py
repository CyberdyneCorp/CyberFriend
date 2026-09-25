"""SQL for scheduled tasks, and the two statements that carry the design.

`CLAIM_DUE` is the first. It selects, advances and returns in one statement, so
a task is claimed exactly once however many sweeps run. The advance happens
*before* the run rather than after it: a crash midway through then costs one
answer instead of looping on the same task for ever, and the task runs again at
its next interval anyway.

It advances to `now() + interval`, not `next_run_at + interval`. That is the
whole of "a missed window is skipped, not replayed" -- an hourly task whose
time passed six times during an outage would otherwise be due six times over
and send six messages the moment the bot came back. The cost is drift of
however long a run takes, which for an hourly rhythm is seconds a day.

`CREATE_WITHIN_CAP` is the second. The cap is a predicate on the insert rather
than a count read and then checked, because two commands arriving together
would each read the same count and both write.
"""

from __future__ import annotations

from sqlalchemy import text

RESOLVE_PERSON_ID = text("""
SELECT person_id FROM person_platform_id
WHERE platform = :platform AND platform_user_id = :platform_user_id
""")

#: Insert only while the person is under their cap.
#:
#: `WHERE (SELECT count(*) ...) < :cap` makes the bound part of the write, so
#: the read and the check cannot be separated by another insert.
CREATE_WITHIN_CAP = text("""
INSERT INTO scheduled_task (person_id, question, interval_hours, next_run_at)
SELECT :person_id, :question, :interval_hours, CAST(:first_run_at AS timestamptz)
WHERE (
    SELECT count(*) FROM scheduled_task WHERE person_id = :person_id
) < :cap
RETURNING id, question, interval_hours, next_run_at, created_at,
          last_run_at, last_outcome, disabled_at, disabled_reason
""")

FOR_PERSON = text("""
SELECT id, question, interval_hours, next_run_at, created_at,
       last_run_at, last_outcome, disabled_at, disabled_reason
FROM scheduled_task
WHERE person_id = :person_id
ORDER BY created_at DESC, id DESC
""")

#: Bound by person as well as by id. Without `person_id` in the WHERE clause
#: this would delete anybody's task by number, and the number is visible in
#: its owner's own listing.
DELETE_OWN = text("""
DELETE FROM scheduled_task WHERE id = :task_id AND person_id = :person_id
""")

#: See the module docstring. `FOR UPDATE SKIP LOCKED` in the subquery is what
#: keeps two sweeps from claiming the same row even though one replica is the
#: intended deployment: the statement should not be correct only by
#: configuration.
#:
#: Opted-out people are skipped as well as purged. `purge_person_derived`
#: deletes their tasks when they opt out; the filter is what keeps a task
#: written afterwards (the table has no insert guard) from ever running.
CLAIM_DUE = text("""
UPDATE scheduled_task s
   SET next_run_at = CAST(:now AS timestamptz)
                     + make_interval(hours => s.interval_hours)
  FROM (
      SELECT id FROM scheduled_task
      WHERE disabled_at IS NULL AND next_run_at <= CAST(:now AS timestamptz)
        AND NOT EXISTS (
            SELECT 1 FROM person_opt_out o WHERE o.person_id = scheduled_task.person_id
        )
      ORDER BY next_run_at
      LIMIT :limit
      FOR UPDATE SKIP LOCKED
  ) due
 WHERE s.id = due.id
RETURNING s.id, s.question, s.person_id,
          (SELECT platform_user_id FROM person_platform_id p
            WHERE p.person_id = s.person_id AND p.platform = :platform
            LIMIT 1) AS platform_user_id
""")

RECORD_RUN = text("""
UPDATE scheduled_task
   SET last_run_at = CAST(:now AS timestamptz), last_outcome = :outcome
 WHERE id = :task_id
""")

#: Stops every task of a person whose direct messages are closed.
#:
#: Kept rather than deleted, like a settled notification: "why did this stop?"
#: is the question this feature will be asked, and a deleted row answers it
#: with silence.
DISABLE_FOR_PERSON = text("""
UPDATE scheduled_task
   SET disabled_at = CAST(:now AS timestamptz), disabled_reason = :reason
 WHERE person_id = :person_id AND disabled_at IS NULL
""")
