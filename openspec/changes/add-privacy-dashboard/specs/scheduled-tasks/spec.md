## MODIFIED Requirements

### Requirement: Removing a person removes their tasks

The system SHALL delete a person's scheduled tasks when their data is removed,
including when they opt out while their person record is kept.

#### Scenario: A person opts out or erases their data
- WHEN a person's data is removed
- THEN their scheduled tasks SHALL be deleted
- AND nothing further SHALL be sent to them

#### Scenario: A due task for a person who has opted out
- WHEN a task falls due for a person who has opted out
- THEN it SHALL NOT run
