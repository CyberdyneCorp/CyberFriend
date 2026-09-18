## Purpose

Let a person have a question asked on their behalf on a rhythm, and be told the
answer, without the assistant becoming a source of unwanted messages.

## ADDED Requirements

### Requirement: A person schedules only their own tasks

A person SHALL create, list and delete only their own scheduled tasks.

#### Scenario: Listing
- WHEN a person lists their scheduled tasks
- THEN only tasks they created SHALL be shown

#### Scenario: Deleting someone else's
- WHEN a person names a task that is not theirs
- THEN it SHALL NOT be deleted
- AND the reply SHALL NOT reveal whether that task exists

### Requirement: The schedule is bounded

An interval SHALL be at least one hour and at most twenty-four, and a person
SHALL NOT hold more than the configured number of tasks.

#### Scenario: Too frequent
- WHEN a person asks for an interval below one hour
- THEN the task SHALL NOT be created
- AND the reply SHALL say what the bounds are

#### Scenario: Too many tasks
- WHEN a person already holds the maximum number of tasks
- THEN a further task SHALL NOT be created

### Requirement: A run is the asker's own question, answered now

A scheduled run SHALL be answered as though the person had just asked it, with
their access resolved at the time of the run.

#### Scenario: Access lost since the task was created
- WHEN a person can no longer read a channel the task used to draw on
- THEN the answer SHALL NOT include anything from that channel

#### Scenario: A state-changing tool
- WHEN a scheduled run would call a state-changing tool
- THEN the call SHALL be refused, because no one is present to approve it

#### Scenario: A question about the corpus
- WHEN a scheduled task asks about channel content
- THEN it SHALL be answered from the channels the person may read

### Requirement: Nothing to say means nothing is sent

The system SHALL send no message for a run that produced no answer.

#### Scenario: A run that finds nothing
- WHEN a scheduled run abstains or produces no answer
- THEN no message SHALL be sent

#### Scenario: A run that fails
- WHEN a scheduled run fails
- THEN no message SHALL be sent
- AND the failure SHALL be recorded against the task

### Requirement: A person can tell a quiet task from a broken one

The listing SHALL show, for each task, when it last ran and what happened.

#### Scenario: A task that has run and found nothing
- WHEN a person lists their tasks
- THEN each SHALL show when it last ran and that it found nothing

#### Scenario: A task that has never run
- WHEN a task has not yet run
- THEN the listing SHALL say when it will

### Requirement: A missed run is skipped, not replayed

The system SHALL run a task at most once per interval, whatever time has
passed.

#### Scenario: The assistant was down for several intervals
- WHEN a task's time passed several times while nothing was running
- THEN it SHALL run once
- AND SHALL NOT send one message per missed interval

### Requirement: Closed direct messages stop a task

The system SHALL stop running a task whose owner cannot be sent direct
messages, and SHALL record why.

#### Scenario: Direct messages closed
- WHEN a person's direct messages cannot be delivered to
- THEN their tasks SHALL stop running
- AND the reason SHALL be visible when they list their tasks

### Requirement: Removing a person removes their tasks

The system SHALL delete a person's scheduled tasks when their data is removed.

#### Scenario: A person opts out or erases their data
- WHEN a person's data is removed
- THEN their scheduled tasks SHALL be deleted
- AND nothing further SHALL be sent to them

### Requirement: Scheduled runs do not consume a person's own allowance

A scheduled run SHALL NOT reduce what a person may ask interactively.

#### Scenario: Tasks running while a person asks a question
- WHEN a person's scheduled tasks run
- THEN their own next question SHALL NOT be refused for rate limiting because
  of them
