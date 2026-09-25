## 1. Storage

- [x] 1.1 Migration: `scheduled_task`, keyed by person, cascading on deletion
- [x] 1.2 Interval bounded in the schema, not only in the command
- [x] 1.3 Partial index on the due set
- [x] 1.4 Atomic claim that advances the schedule before the run
- [x] 1.5 Test: a missed window runs once, not once per interval
- [x] 1.6 Test: deleting a person deletes their tasks

## 2. Running a task

- [x] 2.1 A loop in the bot that claims and runs due tasks
- [x] 2.2 Run through `AskService` as the owner, with no confirmation surface
- [x] 2.3 Send the answer as a direct message
- [x] 2.4 Send nothing when the run abstains or fails
- [x] 2.5 Record the outcome and the time against the task
- [x] 2.6 Scheduled runs do not spend the person's interactive allowance
- [x] 2.7 Stop and record when direct messages are closed
- [x] 2.8 Test: access lost since creation is not drawn on
- [x] 2.9 Test: a state-changing tool is refused
- [x] 2.10 Test: an empty run sends nothing

## 3. The commands

- [x] 3.1 Create, with the interval and the question
- [x] 3.2 List, showing last run, outcome and next run
- [x] 3.3 Delete, by a handle from the listing
- [x] 3.4 Refuse an out-of-bounds interval, saying what the bounds are
- [x] 3.5 Refuse beyond the per-person cap
- [x] 3.6 Another person's task is neither deleted nor revealed
- [x] 3.7 All replies private
- [x] 3.8 Add to the capability reply, in both languages
- [x] 3.9 Test: the listing distinguishes quiet from broken

## 4. Settings, wiring and documentation

- [x] 4.1 Settings: enablement, per-person cap, sweep interval
- [x] 4.2 Declare every setting in `docker-compose.yml`
- [x] 4.3 Wire in `composition.py` and assert the call chain from the entrypoint
- [x] 4.4 README and `docs/operations.md`, including what it costs to run

## 5. Live

- [x] 5.1 Deploy with the feature off, and turn it on deliberately
- [x] 5.2 Confirm a scheduled task runs and delivers (in use in production)
