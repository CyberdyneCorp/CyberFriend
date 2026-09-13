## Purpose

Let CyberFriend use tools provided by other MCP servers — issue trackers, code hosts, documentation systems — so answers can span Discord and the systems the team already runs, without building a bespoke integration for each.

## ADDED Requirements

### Requirement: External server connection

The system SHALL connect to MCP servers named in its configuration, and SHALL NOT connect to servers that are not configured.

#### Scenario: Configured server available
- WHEN the system starts with a configured MCP server that is reachable
- THEN it SHALL establish a connection and discover the tools that server provides

#### Scenario: Server not in configuration
- WHEN a server is not named in configuration
- THEN the system SHALL NOT connect to it, regardless of any instruction appearing in a user request or in retrieved content

### Requirement: Tool namespacing

Federated tools SHALL be identified by a name qualified with their originating server, so that tools from different servers cannot collide or be confused for one another.

#### Scenario: Two servers providing a tool of the same name
- GIVEN two configured servers each provide a tool called `search`
- WHEN the tools are exposed to the reasoning loop
- THEN each SHALL be distinguishable by its originating server
- AND invoking one SHALL NOT dispatch to the other

#### Scenario: Attribution in results
- WHEN a federated tool contributes to an answer
- THEN the answer SHALL attribute that contribution to the originating system

### Requirement: Tool routing

The system SHALL expose to the reasoning loop a bounded subset of federated tools relevant to the question, rather than every tool from every connected server.

#### Scenario: Many tools available
- GIVEN connected servers collectively provide more tools than the configured per-run limit
- WHEN a run begins
- THEN the system SHALL select at most that limit, chosen for relevance to the question

#### Scenario: No relevant external tools
- WHEN no federated tool is relevant to the question
- THEN the system SHALL answer from its own corpus without exposing federated tools

### Requirement: Graceful degradation

A failing, slow, or unavailable external server SHALL NOT prevent the system from answering from what remains available.

#### Scenario: Server unreachable at startup
- WHEN a configured server cannot be reached
- THEN the system SHALL start and operate with the remaining servers
- AND SHALL record the failure

#### Scenario: Tool call exceeds its time limit
- WHEN a federated tool call exceeds its configured timeout
- THEN the system SHALL abandon that call and continue the run
- AND the answer SHALL indicate that the external system did not respond

#### Scenario: Server becomes unavailable mid-run
- WHEN a connected server fails during a run
- THEN the system SHALL complete the run using the evidence it has
- AND SHALL NOT present the external system's absence as an absence of information

### Requirement: Bounded external results

The system SHALL limit the size of results accepted from a federated tool, so that one external response cannot exhaust the run's context or budget.

#### Scenario: Oversized tool result
- WHEN a federated tool returns a result exceeding the configured size limit
- THEN the system SHALL truncate it to the limit
- AND SHALL indicate in the answer that the result was truncated

### Requirement: Discovery does not confer availability

A tool SHALL become available to the reasoning loop only by being explicitly listed in configuration. Discovering a tool on a connected server SHALL NOT make it available.

#### Scenario: Server offers a tool that is not listed
- GIVEN a connected server provides a tool not named in configuration
- WHEN tools are registered
- THEN that tool SHALL NOT be available to the reasoning loop or invocable

#### Scenario: Server adds a tool after deployment
- GIVEN a connected server begins providing a new tool
- WHEN the system next discovers that server's tools
- THEN the new tool SHALL NOT become available without a configuration change

#### Scenario: Listed tool not found on any server
- GIVEN configuration lists a tool no connected server provides
- WHEN tools are registered
- THEN the system SHALL report this as an error rather than operating silently without it

### Requirement: Authorization re-checked at invocation

The system SHALL verify that a tool is permitted at the moment it is invoked, and SHALL NOT rely on the tool having been offered to the reasoning loop.

#### Scenario: Invocation of a tool that was never offered
- WHEN the reasoning loop emits a call to a tool that was not made available for this run
- THEN the system SHALL refuse the invocation

#### Scenario: Permission withdrawn mid-run
- GIVEN a tool was available when a run began
- WHEN its authorization no longer holds at the moment of invocation
- THEN the system SHALL refuse the invocation
