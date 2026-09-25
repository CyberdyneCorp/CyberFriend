## MODIFIED Requirements

### Requirement: The corpus is not reachable through the console

The console SHALL configure the agent and SHALL NOT expose message, document or
ask content. The single exception SHALL be the text of the questions a person
has asked the assistant, which only the admin role may read, as the
`usage-reporting` capability describes. Answers and quoted evidence SHALL NOT
be exposed.

#### Scenario: Requesting content
- WHEN a request asks for message, document or ask content
- THEN the system SHALL refuse it

#### Scenario: Diagnostics
- WHEN the console reports ingestion progress or usage
- THEN it MAY report counts and timings
- AND SHALL NOT include content or excerpts

#### Scenario: Question text
- WHEN an admin requests the questions a person asked
- THEN the system MAY return the questions themselves
- AND SHALL NOT return the answers or any message they quote
