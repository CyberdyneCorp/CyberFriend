## ADDED Requirements

### Requirement: A person can see what is held about them

The privacy command SHALL show the person, and only the person, what the
assistant holds about them: their facts, remembered conversation, scheduled
tasks, alerts, notification setting, voice usage, suggestions, access tokens,
whether their messages are archived and in which channels they can currently
read, and how many of their questions are traced.

#### Scenario: Using the command in a DM
- WHEN a person uses the privacy command in a direct message
- THEN the assistant SHALL list each kind of data with its values

#### Scenario: Using the command in a server channel
- WHEN a person uses the privacy command in a server channel
- THEN the reply SHALL be visible only to them
- AND SHALL show counts and fact kinds but no fact values
- AND SHALL offer to send the details by direct message

#### Scenario: Channels the person can no longer read
- WHEN some of the person's archived messages are in channels they can no longer
  read
- THEN the assistant SHALL show at most one total for those channels
- AND SHALL NOT name them

### Requirement: The dashboard states what is kept and who can see it

The privacy reply SHALL state that the person's questions are traced and
readable by admins for the retention period, that other people's messages
mentioning them are not deleted, and that voice minutes are kept for
accounting.

#### Scenario: Reading the dashboard
- WHEN the privacy reply is shown
- THEN it SHALL include those statements in the person's language

### Requirement: A person can delete everything with one confirmed action

The assistant SHALL offer a single delete-everything action that, after the
person types a confirmation word, opts them out and erases every stored item
about them that the design does not list as kept, including their traced
questions and traces that quote their messages. Before confirming, the person
SHALL be told that this stops archiving, memory, voice questions and tracing
and cannot be undone, and SHALL be offered the lighter forget command.

#### Scenario: Confirmed deletion
- WHEN a person presses delete everything and types the confirmation word
- THEN their messages, facts, memory, tasks, alerts, notifications,
  suggestions and tokens SHALL be deleted
- AND their traces SHALL be scheduled for deletion from the trace store
- AND the reply SHALL give counts and say that traces are scheduled for
  deletion

#### Scenario: Wrong confirmation word
- WHEN the typed word does not match
- THEN nothing SHALL be deleted

#### Scenario: Someone else presses the button
- WHEN a person other than the requester presses the delete button
- THEN it SHALL have no effect

#### Scenario: Messages are not re-imported
- WHEN a person has deleted everything and the channel is backfilled again
- THEN their messages SHALL NOT be archived again

### Requirement: An erasure finishes even if interrupted

An erasure SHALL be recorded durably before it starts and SHALL be resumed
until complete if it is interrupted or the trace store is unavailable.

#### Scenario: Crash partway through
- WHEN the process stops after some erasure steps have run
- THEN a later sweep SHALL complete the remaining steps

#### Scenario: Trace store unreachable
- WHEN the trace store cannot be reached during erasure
- THEN the trace deletions SHALL stay pending and be retried
- AND the rest of the erasure SHALL still take effect
