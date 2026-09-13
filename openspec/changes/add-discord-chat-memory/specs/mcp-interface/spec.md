## Purpose

Expose the corpus to clients as a small set of MCP tools, so that a Discord bot, a Slack bot, or a developer's agent session all query one implementation with one permission model rather than each reimplementing retrieval.

## ADDED Requirements

### Requirement: Viewer identity on every tool call

Every tool that returns message content SHALL require the identity of the person on whose behalf it is called, and SHALL scope its results to that person's permissions.

#### Scenario: Tool called with a viewer
- WHEN a client calls a content-returning tool supplying a viewer identity
- THEN results SHALL be restricted to channels that viewer may read

#### Scenario: Tool called without a viewer
- WHEN a client calls a content-returning tool without supplying a viewer identity
- THEN the call SHALL fail with an error
- AND SHALL NOT return message content

#### Scenario: Client requests a different viewer's access
- WHEN a client supplies a viewer identity
- THEN the results SHALL reflect that viewer's permissions only, and SHALL NOT be broadened by any other parameter of the call

### Requirement: Message search tool

The interface SHALL provide a tool that searches the corpus on behalf of a viewer, optionally constrained by channel and time range.

#### Scenario: Search with a query
- WHEN a client searches with a query on behalf of a viewer
- THEN the tool SHALL return matching results with their citations, ordered by relevance

#### Scenario: Search constrained to a channel
- WHEN a client searches constrained to a named channel the viewer may read
- THEN all results SHALL come from that channel

#### Scenario: Search constrained to a channel the viewer cannot read
- WHEN a client searches constrained to a channel the viewer may not read
- THEN the tool SHALL return no results
- AND the response SHALL NOT reveal whether that channel exists or contains matching content

### Requirement: Thread context tool

The interface SHALL provide a tool that returns the conversation surrounding a cited message, so a client can expand a search result into its context.

#### Scenario: Context requested for a visible message
- WHEN a client requests the context of a message the viewer may read
- THEN the tool SHALL return the surrounding conversation with citations

#### Scenario: Context requested for a message the viewer cannot read
- WHEN a client requests the context of a message the viewer may not read
- THEN the tool SHALL return an error indicating the message is unavailable
- AND SHALL NOT return any surrounding content

### Requirement: Channel listing tool

The interface SHALL provide a tool listing the indexed channels available to a viewer, so clients can offer meaningful scoping options.

#### Scenario: Listing channels
- WHEN a client lists channels on behalf of a viewer
- THEN the tool SHALL return only indexed channels that viewer may read
- AND SHALL omit channels outside indexing scope

### Requirement: Explicit empty results

The interface SHALL distinguish a query with no matches from a query that failed, so clients do not present an error as an absence of activity.

#### Scenario: Query with no matches
- WHEN a query completes successfully but matches nothing
- THEN the tool SHALL return an empty result set rather than an error

#### Scenario: Retrieval failure
- WHEN a query cannot be completed because a dependency is unavailable
- THEN the tool SHALL return an error rather than an empty result set
