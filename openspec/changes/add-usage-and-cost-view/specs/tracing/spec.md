## ADDED Requirements

### Requirement: A trace names its feature, tools and model usage

Each exported run SHALL be named by a stable feature identifier and tagged with
its feature, path, answer language and the tools it called. It SHALL carry one
record per model call with the stage, model and input and output tokens, and
one record per federated tool call with the tool name, outcome and latency.
Tool arguments SHALL NOT be exported.

#### Scenario: A market price question
- WHEN a traced run answers a market price question
- THEN its trace SHALL be named `market.price` and tagged with that feature

#### Scenario: A run that calls a federated tool
- WHEN a run calls a federated tool with arguments
- THEN the trace SHALL record the tool's name and outcome
- AND SHALL NOT contain the arguments

#### Scenario: Cost can be computed
- WHEN a run makes model calls
- THEN each call SHALL be exported with its model and its input and output
  token counts

### Requirement: Every answer path is traced

Every answer the assistant gives to a question SHALL be traced when tracing is
enabled, including answers about its own capabilities, obligations and
decisions, subject to the opt-out rule.

#### Scenario: Capabilities question
- WHEN someone asks what the assistant can do and tracing is enabled
- THEN the run SHALL be exported with feature `capabilities`

### Requirement: Traces are kept for a stated period

The system SHALL delete exported traces older than the configured retention
period (default 90 days), including traces it has no local record of.

#### Scenario: A trace passes the retention period
- WHEN a trace becomes older than the retention period
- THEN the system SHALL request its deletion from the trace store

#### Scenario: Trace with no local index
- WHEN an old trace exists in the trace store without a local export record
- THEN the retention sweep SHALL still request its deletion

### Requirement: People are told that their questions are visible to admins

Before question text is visible in the console, the assistant's description of
itself and the privacy dashboard SHALL state that admins can read the questions
a person asks, for how long they are kept, and how to delete them.

#### Scenario: Asking what the assistant can do
- WHEN someone asks what the assistant does
- THEN the reply SHALL state that admins can read asked questions, the
  retention period, and that `/privacy` deletes them
