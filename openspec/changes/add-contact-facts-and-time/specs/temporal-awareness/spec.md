## Purpose

Let the assistant know when "now" is, so an answer about time is grounded in
the clock rather than in what a model assumes the date to be.

## ADDED Requirements

### Requirement: The current time is given to the answering prompt

The system SHALL tell the answering prompt the current date and time, and SHALL
state which zone it is in.

#### Scenario: Answering any question
- WHEN a question is answered
- THEN the prompt SHALL carry the current date and time
- AND that time SHALL be labelled with its zone

#### Scenario: Asked the date
- WHEN a person asks what the date or time is
- THEN the answer SHALL come from that clock

### Requirement: The clock is not evidence

The current time SHALL NOT be citable, and SHALL NOT make an ungrounded claim
answerable.

#### Scenario: A question the corpus cannot answer
- WHEN a question needs evidence the corpus does not hold
- THEN knowing the time SHALL NOT turn an abstention into an answer

#### Scenario: Citations
- WHEN an answer is produced
- THEN the current time SHALL NOT appear as a cited source

### Requirement: A stated time says which zone it is

Any time the assistant states SHALL carry its zone.

#### Scenario: Reporting a time
- WHEN an answer names a date or time
- THEN it SHALL say which zone that time is in
