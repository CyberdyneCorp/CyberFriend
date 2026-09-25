## ADDED Requirements

### Requirement: A person can see what is held about them

The privacy command SHALL show the person, and only the person, what the
assistant holds about them: their facts, remembered conversation, scheduled
tasks, alerts, notification setting, voice usage, attachments and
transcripts of their messages, suggestions, access tokens, whether their
messages are archived and in which channels they can currently read, and how
many of their questions are traced.

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
- THEN the assistant SHALL NOT name those channels or count messages in them

### Requirement: The dashboard states what is recorded and what is kept

The privacy reply SHALL state that the person's questions and the assistant's
answers are recorded for up to the retention period and readable by admins. It
SHALL list everything that survives a deletion: a minimal person record, the
change log entries that refer to them, database backups until they age out
with the backup retention period, a linked identity-provider account (which
cannot yet be deleted from here), other people's messages that mention them,
other people's remembered answers that may paraphrase what they said and
expire within the memory retention period, messages the assistant already
sent, and an anonymous voice-usage total.

#### Scenario: Reading the dashboard
- WHEN the privacy reply is shown
- THEN it SHALL include those statements in the person's language

### Requirement: A person can delete everything with one confirmed action, and choose whether to leave

The assistant SHALL offer one delete-everything flow with two choices: delete
everything and keep using the assistant, or delete everything and stop being
archived. After the person types a confirmation word, either choice SHALL erase
every stored item about them that the kept list does not name, including
their messages and message media, their voice usage records, their traced
questions and traces that quote their messages. Before confirming, the person
SHALL be shown what is deleted, what is kept, what each choice means, and that
it cannot be undone.

#### Scenario: Delete everything and keep using the assistant
- WHEN a person chooses delete everything and types the confirmation word
- THEN their messages, message media, facts, memory, tasks, alerts,
  notifications, suggestions, tokens and voice usage records SHALL be deleted
- AND their traces SHALL be scheduled for deletion from the trace store
- AND the reply SHALL give counts and say that traces are scheduled for
  deletion
- AND messages they send afterwards SHALL be archived as usual

#### Scenario: Delete everything and stop archiving
- WHEN a person chooses delete everything and stop archiving me and types the
  confirmation word
- THEN everything SHALL be deleted as for the first choice
- AND the person SHALL be opted out, so that nothing they send later is
  archived, remembered or traced

#### Scenario: Voice caps stay true
- WHEN a person's voice usage records are deleted
- THEN their seconds SHALL remain in an anonymous monthly total
- AND the server-wide monthly limit SHALL be unchanged

#### Scenario: Wrong confirmation word
- WHEN the typed word does not match
- THEN nothing SHALL be deleted

#### Scenario: Someone else presses the button
- WHEN a person other than the requester presses a delete button
- THEN it SHALL have no effect

#### Scenario: Messages are not re-imported
- WHEN a person has deleted everything, with either choice, and the channel is
  backfilled again
- THEN their messages from before the deletion SHALL NOT be archived again

#### Scenario: Other people's data is not touched
- WHEN a person deletes everything
- THEN other people's messages and remembered answers SHALL NOT be deleted or
  changed

### Requirement: Opt-out and erasure share one delete path

Every store that holds data about a person SHALL be purged by the same
mechanism whether the person is opted out by an admin, opts out through the
delete flow, or erases without opting out.

#### Scenario: A store added later
- WHEN a new store of personal data is added
- THEN it SHALL be purged by opt-out and by erasure alike without a separate
  erasure step

### Requirement: An erasure finishes even if interrupted

An erasure SHALL be recorded durably before it starts, SHALL stop re-import
before purging anything, and SHALL be resumed until complete if it is
interrupted or the trace store is unavailable.

#### Scenario: Crash partway through
- WHEN the process stops after some erasure steps have run
- THEN a later sweep SHALL complete the remaining steps

#### Scenario: Trace store unreachable
- WHEN the trace store cannot be reached during erasure
- THEN the trace deletions SHALL stay pending and be retried
- AND the rest of the erasure SHALL still take effect
