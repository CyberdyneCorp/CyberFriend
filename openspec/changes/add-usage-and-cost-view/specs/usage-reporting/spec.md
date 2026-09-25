## ADDED Requirements

### Requirement: Usage is reported per person and per feature

The console SHALL report, for a chosen window of up to 90 days, the number of
traced questions, input and output tokens, estimated cost and tools used,
grouped by person, by feature, by model or by tool. Voice minutes SHALL be
reported from the voice usage ledger.

#### Scenario: Operator opens the usage view
- WHEN a person with the operator role opens the usage view for the last 30
  days
- THEN they SHALL see counts, tokens, cost and tools per person and per feature
- AND each person SHALL be shown by their current display name, or by their
  platform id when no name is known

#### Scenario: Window too long
- WHEN a request asks for more than 90 days
- THEN the system SHALL refuse it

#### Scenario: Totals are labelled
- WHEN usage totals are shown
- THEN they SHALL be labelled as traced question runs and show when they were
  last synchronised

### Requirement: Counts do not depend on the trace store being available

Usage counts SHALL be served from a local rollup that holds no question,
answer or evidence text. The rollup SHALL be refreshed periodically from the
trace store, and recent days SHALL be recomputed so that late writes and
deletions are reflected.

#### Scenario: Trace store unreachable
- WHEN the trace store cannot be reached
- THEN the usage view SHALL still show the last synchronised counts

#### Scenario: A trace is deleted after synchronisation
- WHEN a trace from the last 48 hours is deleted
- THEN a later synchronisation SHALL remove it from the counts

### Requirement: Question text is for admins only, and only the asker's own words

The system SHALL return the text of a person's questions only to the admin
role. It SHALL return only the question, its time, feature, tools, tokens and
cost, and SHALL NOT return the answer, the evidence or any other trace
content.

#### Scenario: Operator requests question text
- WHEN a person with the operator role requests another person's questions
- THEN the system SHALL refuse it as forbidden

#### Scenario: Admin reads questions
- WHEN an admin requests a person's questions
- THEN the response SHALL contain only the asker's questions and their
  metadata
- AND SHALL NOT contain answers or quoted messages

#### Scenario: Viewing is recorded
- WHEN an admin views a person's questions
- THEN the system SHALL record who viewed whose questions and for which window

#### Scenario: Not kept by the browser
- WHEN question text is returned
- THEN the response SHALL forbid caching and the console SHALL NOT store it

### Requirement: Trace store credentials stay server-side

The trace store's keys SHALL be held only by server processes and SHALL NOT be
sent to the browser.

#### Scenario: Inspecting console traffic
- WHEN any console response is inspected
- THEN it SHALL NOT contain the trace store's keys or address credentials
