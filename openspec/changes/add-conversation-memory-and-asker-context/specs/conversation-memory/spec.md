## Purpose

Let a person hold a conversation with the assistant, so follow-ups resolve,
while keeping what is remembered private to that person, inside their current
permissions, and under their control.

## ADDED Requirements

### Requirement: A conversation belongs to one person in one place

The system SHALL keep conversation history per person and per location, and
SHALL NOT use one person's history when answering another.

#### Scenario: Two people in the same channel
- WHEN two people ask questions in the same channel
- THEN each person's follow-up SHALL be interpreted using only their own
  earlier turns

#### Scenario: A direct message and a channel
- WHEN a person has a conversation in a direct message and asks in a channel
- THEN the direct-message history SHALL NOT be used for the channel answer

### Requirement: History survives restarts

The system SHALL persist conversation history so that a restart does not end a
conversation.

#### Scenario: Restart between questions
- WHEN the service restarts between a question and its follow-up
- THEN the follow-up SHALL still be interpreted with the earlier turn

### Requirement: Answers are remembered with their provenance

The system SHALL store the assistant's answers alongside the questions, together
with the channels each answer drew on.

#### Scenario: Answer recorded
- WHEN the assistant answers a question
- THEN the stored turn SHALL include the answer and the channels its citations
  came from

### Requirement: Remembered turns respect current permissions

Before a remembered turn is used, the system SHALL check the channels it drew on
against what the person can read at that moment, and SHALL discard the turn if
any of them is no longer readable.

#### Scenario: Access revoked after an answer
- WHEN a person received an answer drawing on a channel and later loses access
  to it
- THEN that turn SHALL NOT be used when answering their next question

#### Scenario: Summary drawing on a revoked channel
- WHEN a summary covers turns that drew on a channel the person can no longer
  read
- THEN the summary SHALL NOT be used

### Requirement: Long conversations are summarised

When a conversation exceeds a bound, the system SHALL replace its older turns
with a summary that records the union of the channels those turns drew on.

#### Scenario: Conversation grows past the bound
- WHEN a conversation exceeds the configured number of turns
- THEN older turns SHALL be replaced by a summary
- AND the most recent turns SHALL be kept verbatim

### Requirement: Memory is context, never evidence

Remembered turns SHALL inform how a question is interpreted and SHALL NOT be
cited or used as the source of a factual claim.

#### Scenario: Follow-up needing fresh evidence
- WHEN a follow-up depends on an earlier answer
- THEN the new answer SHALL be grounded in freshly retrieved evidence, not in
  the remembered answer text

#### Scenario: Instruction inside a remembered turn
- WHEN a remembered question or answer contains text shaped as an instruction
- THEN that text SHALL have no effect

### Requirement: People control their memory

The system SHALL let a person erase their conversation history, SHALL expire
history after a retention window, and SHALL keep no history for a person who
has opted out.

#### Scenario: Forget command
- WHEN a person asks the assistant to forget their conversation
- THEN their history and summaries SHALL be deleted

#### Scenario: Retention window passes
- WHEN a turn is older than the retention window
- THEN it SHALL be deleted

#### Scenario: Opted-out person
- WHEN a person has opted out
- THEN the system SHALL store no conversation history for them
- AND SHALL delete any history that already exists
