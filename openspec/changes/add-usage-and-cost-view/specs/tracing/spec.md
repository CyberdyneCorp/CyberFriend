## ADDED Requirements

### Requirement: A trace names its feature, tools and model usage

Each exported run SHALL be named by a stable feature identifier and tagged with
this application's tag, its environment, its feature, path, answer language and
the tools it called. It SHALL carry one record per model call with the stage,
model and input and output tokens, and one record per federated tool call with
the tool name, outcome and latency. Tool arguments SHALL NOT be exported.

#### Scenario: A market price question
- WHEN a traced run answers a market price question
- THEN its trace SHALL be named `market.price` and tagged with that feature and
  with this application's tag

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

#### Scenario: Catch-up and said-by questions
- WHEN someone asks what they missed in a channel, or what a named person said,
  and tracing is enabled
- THEN the run SHALL be exported with feature `corpus.catchup` or
  `corpus.said_by` respectively

### Requirement: Traces are kept for a stated period, and only ours are deleted

The system SHALL delete this application's exported traces older than the
configured retention period (default 90 days), including traces it has no
local record of. It SHALL NOT delete traces of any other application or
environment in the same trace store.

#### Scenario: A trace passes the retention period
- WHEN a trace becomes older than the retention period
- THEN the system SHALL request its deletion from the trace store

#### Scenario: Trace with no local index
- WHEN an old trace of this application exists in the trace store without a
  local export record
- THEN the retention sweep SHALL still request its deletion

#### Scenario: Another environment's old trace
- WHEN an old trace in the trace store belongs to another environment or
  application
- THEN the retention sweep SHALL leave it untouched

### Requirement: People are told actively that their questions and answers are recorded

When tracing is enabled, the first traced reply the assistant gives a person
after this notice takes effect (a question answered through the answer path,
in a direct message, a channel or the ask command) SHALL carry a one-time
notice in the person's language (English or Portuguese; the question's
language, else the person's saved preferred language, else English) stating
that their questions and the assistant's answers are recorded for up to the
retention period, that admins can read them, and that the privacy command
shows how many are kept and deletes them. Replies that do not reach the
answer path (personal fact requests, alert proposals, typed commands, bare
mentions) SHALL NOT carry it. The system SHALL record that the person received
it and SHALL NOT repeat it until the notice changes. The notice SHALL NOT be
stored as part of the remembered conversation turn. The assistant's
description of itself SHALL state the same.

#### Scenario: First traced reply after the change
- WHEN a person who has not received the notice gets a traced reply
- THEN the reply SHALL carry the notice in their language
- AND the system SHALL record that they received it
- AND the remembered turn SHALL NOT contain the notice

#### Scenario: Later replies
- WHEN a person who has received the current notice gets another reply
- THEN the reply SHALL NOT carry the notice

#### Scenario: Tracing disabled
- WHEN tracing is disabled and a person gets a reply
- THEN the reply SHALL NOT carry the notice
- AND nothing SHALL be recorded for the person

#### Scenario: Opted-out person
- WHEN a person who opted out of indexing gets a reply
- THEN the reply SHALL NOT carry the notice
- AND nothing SHALL be recorded for the person

#### Scenario: Scheduled run
- WHEN a scheduled question is answered and delivered
- THEN the delivery SHALL NOT carry the notice
- AND the notice SHALL NOT be recorded as received

#### Scenario: Asking what the assistant can do
- WHEN someone asks what the assistant does and tracing is enabled
- THEN the reply SHALL state that questions and answers are recorded, the
  retention period, that admins can read them, and that the privacy command
  shows how many are kept and deletes them

## MODIFIED Requirements

### Requirement: A trace carries the question, the answer and the evidence

An exported trace SHALL include the question as asked, the answer as sent, the
run's path, status, terminal cause and spend, and a reference to each piece of
retrieved evidence the run held (its window, channel, source system and score).
It SHALL NOT include the text of the evidence or any excerpt of it.

#### Scenario: An answered run
- WHEN a run produces an answer
- THEN its trace SHALL contain the question text, the answer text, and a
  reference to each piece of evidence with its channel and source system
- AND SHALL NOT contain the evidence text

#### Scenario: A run that answered from nothing
- WHEN a run abstains
- THEN its trace SHALL still record the question and the terminal cause
- AND SHALL record that no evidence was held
