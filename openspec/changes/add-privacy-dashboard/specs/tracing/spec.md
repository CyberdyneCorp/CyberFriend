## ADDED Requirements

### Requirement: Opting out withdraws a person's existing traces

When a person opts out or deletes everything, with either choice, the system SHALL request deletion
of every trace of a question they asked and every trace quoting a message they
authored, including traces exported before the system recorded who asked them.

#### Scenario: Opted out by an admin
- WHEN an admin opts a person out
- THEN the traces of that person's questions SHALL be scheduled for deletion

#### Scenario: Trace exported before askers were recorded
- WHEN a person's trace has no local record of its asker
- THEN the system SHALL find it in the trace store by the person's platform id,
  among this application's traces only, and schedule it for deletion

#### Scenario: Trace store refuses the deletion
- WHEN the trace store rejects or cannot receive the deletion
- THEN it SHALL stay pending and be retried
