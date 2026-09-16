## Purpose

Send a question to the web or an MCP server when that is what it needs, while
keeping the team's own conversations first and every outbound query bounded to
the asker's words.

## ADDED Requirements

### Requirement: An explicit request skips the corpus

A question that explicitly asks to search the web SHALL go to external sources
without first being answered from the corpus.

#### Scenario: Explicit web request
- WHEN a person asks "search the web for the latest Python release"
- THEN the answer SHALL come from external sources

### Requirement: A weak corpus answer falls back

When corpus evidence is insufficient to answer, and not only when it is empty,
the system SHALL try external sources before abstaining.

#### Scenario: Corpus returns loosely related messages
- WHEN retrieval returns messages that do not answer the question
- THEN the system SHALL consult external sources before answering

#### Scenario: Corpus answers well
- WHEN corpus evidence answers the question
- THEN the answer SHALL come from the corpus and external sources SHALL NOT be
  consulted

### Requirement: External answers are labelled

An answer drawing on the web or an MCP server SHALL say so and SHALL link to its
source.

#### Scenario: Web citation
- WHEN an answer cites a web result
- THEN the citation SHALL include a working link to that result

### Requirement: MCP servers are configured by operators only

The system SHALL NOT add, remove or reconfigure an MCP server in response to a
chat message.

#### Scenario: Request to add a server from chat
- WHEN a person asks the assistant to connect to an MCP server
- THEN the system SHALL refuse and point to the admin console
