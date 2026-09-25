# tracing Specification

## Purpose
Keep enough of each run to study later -- what was asked, what was answered,
and what the answer rested on -- without weakening a guarantee the corpus
already makes.
## Requirements
### Requirement: Tracing is off until a destination is configured

The system SHALL NOT send any trace unless an operator has configured a
tracing destination, and SHALL start normally when none is configured.

#### Scenario: No destination configured
- WHEN no tracing destination is configured
- THEN no run SHALL be exported
- AND the assistant SHALL answer questions as normal

#### Scenario: Destination configured
- WHEN a destination and its credentials are configured
- THEN each run SHALL be exported to it

### Requirement: A trace carries the question, the answer and the evidence

An exported trace SHALL include the question as asked, the answer as sent, the
run's path, status, terminal cause and spend, and the retrieved evidence the
run held.

#### Scenario: An answered run
- WHEN a run produces an answer
- THEN its trace SHALL contain the question text, the answer text, and each
  piece of evidence with its channel, source system and text

#### Scenario: A run that answered from nothing
- WHEN a run abstains
- THEN its trace SHALL still record the question and the terminal cause
- AND SHALL record that no evidence was held

### Requirement: Tracing never changes an answer

A trace SHALL be produced from a finished run, and a tracing failure SHALL NOT
alter, delay past its timeout, or fail the reply.

#### Scenario: The destination is unreachable
- WHEN the tracing destination cannot be reached
- THEN the person SHALL receive the same answer they would have received
- AND the failure SHALL be logged and not raised

#### Scenario: The destination is slow
- WHEN the destination does not respond within its timeout
- THEN the export SHALL be abandoned rather than held open

### Requirement: Deleting content deletes the traces that quote it

When a message is deleted from the corpus, the system SHALL delete every
exported trace whose evidence quotes that message.

#### Scenario: A traced message is deleted
- WHEN a message that appeared as evidence in a trace is deleted
- THEN that trace SHALL be deleted from the destination

#### Scenario: The destination cannot be reached at deletion time
- WHEN the deletion cannot be delivered
- THEN it SHALL be retried rather than dropped
- AND the corpus deletion SHALL still take effect

### Requirement: Traces are not a second answering surface

An exported trace SHALL NOT be readable through the assistant, and SHALL NOT be
retrievable as evidence for any question.

#### Scenario: Asking about traces
- WHEN someone asks the assistant about traced runs
- THEN the traces SHALL NOT be searched or cited

### Requirement: A person who has opted out is not traced

The system SHALL NOT export a run whose asker has opted out of indexing.

#### Scenario: Opted-out asker
- WHEN a person who has opted out asks a question
- THEN their question and its answer SHALL NOT be exported

