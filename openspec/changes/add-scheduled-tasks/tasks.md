## 1. Storage

- [ ] 1.1 Migration: `scheduled_task`, keyed by person, cascading on deletion
- [ ] 1.2 Interval bounded in the schema, not only in the command
- [ ] 1.3 Partial index on the due set
- [ ] 1.4 Atomic claim that advances the schedule before the run
- [ ] 1.5 Test: a missed window runs once, not once per interval
- [ ] 1.6 Test: deleting a person deletes their tasks

## 2. Running a task

- [ ] 2.1 A loop in the bot that claims and runs due tasks
- [ ] 2.2 Run through `AskService` as the owner, with no confirmation surface
- [ ] 2.3 Send the answer as a direct message
- [ ] 2.4 Send nothing when the run abstains or fails
- [ ] 2.5 Record the outcome and the time against the task
- [ ] 2.6 Scheduled runs do not spend the person's interactive allowance
- [ ] 2.7 Stop and record when direct messages are closed
- [ ] 2.8 Test: access lost since creation is not drawn on
- [ ] 2.9 Test: a state-changing tool is refused
- [ ] 2.10 Test: an empty run sends nothing

## 3. The commands

- [ ] 3.1 Create, with the interval and the question
- [ ] 3.2 List, showing last run, outcome and next run
- [ ] 3.3 Delete, by a handle from the listing
- [ ] 3.4 Refuse an out-of-bounds interval, saying what the bounds are
- [ ] 3.5 Refuse beyond the per-person cap
- [ ] 3.6 Another person's task is neither deleted nor revealed
- [ ] 3.7 All replies private
- [ ] 3.8 Add to the capability reply, in both languages
- [ ] 3.9 Test: the listing distinguishes quiet from broken

## 4. Settings, wiring and documentation

- [ ] 4.1 Settings: enablement, per-person cap, sweep interval
- [ ] 4.2 Declare every setting in `docker-compose.yml`
- [ ] 4.3 Wire in `composition.py` and assert the call chain from the entrypoint
- [ ] 4.4 README and `docs/operations.md`, including what it costs to run

## 5. Live

- [ ] 5.1 Deploy with the feature off, and turn it on deliberately
- [ ] 5.2 Confirm a scheduled task runs and delivers
