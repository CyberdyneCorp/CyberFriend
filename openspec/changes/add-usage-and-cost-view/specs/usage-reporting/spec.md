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
- THEN they SHALL be labelled as traced question runs

### Requirement: Usage is read live from the trace store for this app only

Usage SHALL be read from the trace store at request time, server-side, scoped
to this application's tag and environment, and MAY be cached in the server
process for at most five minutes. No local copy of usage SHALL be kept. When
the trace store cannot be reached, the view SHALL say that usage is
unavailable.

#### Scenario: Trace store unreachable
- WHEN the trace store cannot be reached
- THEN the usage view SHALL say that usage is unavailable
- AND SHALL NOT show stale or partial counts as current

#### Scenario: Another environment shares the trace store
- WHEN the trace store holds traces from another environment or application
- THEN they SHALL NOT be counted or shown

### Requirement: People who left or erased their data never appear

Before returning counts or text, the system SHALL remove, using its own
records at the time of the request, every person who has opted out, every
person with an erasure in progress, and, for a person who has erased their
data, everything up to the time of the erasure. It SHALL also remove any trace
whose deletion has been requested. This SHALL hold even while the trace store
still holds those traces.

#### Scenario: Erased person's questions are not shown
- WHEN a person has deleted everything and the trace store has not yet purged
  their traces
- THEN their earlier questions SHALL NOT be returned
- AND their earlier usage SHALL NOT be counted

#### Scenario: Opted-out person
- WHEN a person has opted out
- THEN they SHALL NOT appear in any usage count or question list

#### Scenario: A trace pending deletion
- WHEN a trace's deletion has been requested and not yet carried out
- THEN it SHALL NOT be returned as question text

#### Scenario: Opting out after the view was cached
- WHEN a person opts out while usage counts are cached
- THEN the next request SHALL no longer include them

### Requirement: Question text is for signed-in admins only, and only the asker's own words

The system SHALL return the text of a person's questions only to a person with
the admin role signed in through the identity provider, and never to an
operator or to a static token. It SHALL return only the question, its time,
feature, tools, tokens and cost, and SHALL NOT return the answer, the evidence
or any other trace content.

#### Scenario: Operator requests question text
- WHEN a person with the operator role requests another person's questions
- THEN the system SHALL refuse it as forbidden

#### Scenario: Script token requests question text
- WHEN a request authenticated by a static token requests a person's questions
- THEN the system SHALL refuse it as forbidden

#### Scenario: Admin reads questions
- WHEN a signed-in admin requests a person's questions
- THEN the response SHALL contain only the asker's questions and their
  metadata
- AND SHALL NOT contain answers or quoted messages

#### Scenario: Viewing is recorded
- WHEN an admin views a person's questions
- THEN the system SHALL record the admin's subject, which person's questions
  were viewed, and the window

#### Scenario: Not kept by the browser
- WHEN question text is returned
- THEN the response SHALL forbid caching and the console SHALL NOT store it

### Requirement: Question text is shown only after the person was told

The system SHALL show as text only questions traced after the person received
the disclosure notice. Earlier traces SHALL be counted and SHALL NOT be shown.

#### Scenario: Traces from before the notice
- WHEN an admin views a person's questions and some were traced before that
  person received the notice
- THEN those SHALL be reported only as a count

#### Scenario: A person who never received the notice
- WHEN a person has not yet received the notice
- THEN none of their questions SHALL be shown as text

### Requirement: Trace store credentials stay server-side

The trace store's keys SHALL be held only by server processes and SHALL NOT be
sent to the browser.

#### Scenario: Inspecting console traffic
- WHEN any console response is inspected
- THEN it SHALL NOT contain the trace store's keys or address credentials
