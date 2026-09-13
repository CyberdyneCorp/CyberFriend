## Purpose

Guarantee that the agent's authority to act always derives from the person who asked, and never from content the agent has read — so that indexing messages written by anyone in the server can never become a way to drive the agent's tools, credentials, or output.

## ADDED Requirements

### Requirement: Retrieved content is data, never instruction

The system SHALL treat all retrieved message content, and all results returned by federated tools, as data to reason about. Text within such content SHALL NOT be followed as direction, whatever form it takes.

#### Scenario: Indexed message containing instructions
- GIVEN an indexed message whose text instructs the reader to perform an action, disclose other content, or disregard prior direction
- WHEN that message is retrieved into a reasoning run
- THEN the system SHALL treat it as message content to be reported on
- AND SHALL NOT perform the action it describes

#### Scenario: Federated tool result containing instructions
- GIVEN a federated tool returns content that instructs the reader to take an action
- WHEN that result enters a reasoning run
- THEN the system SHALL NOT perform the action it describes

#### Scenario: Content attempting to alter the viewer's permissions
- GIVEN retrieved content that asserts the requesting person is an administrator, or is authorised to access other channels
- WHEN that content enters a reasoning run
- THEN it SHALL have no effect on the permissions applied to that run

### Requirement: Authority derives from the requesting person

Every action the agent takes SHALL be attributable to the request of an identified person, and SHALL be performed within that person's authority.

#### Scenario: Tool call serving the viewer's request
- WHEN the system invokes a federated tool while answering a person's question
- THEN the invocation SHALL be recorded as made on that person's behalf

#### Scenario: Action originating from retrieved content
- WHEN a candidate action does not serve the requesting person's own question but arises from retrieved content
- THEN the system SHALL NOT perform it

#### Scenario: Agent authority exceeding the requester's
- WHEN a federated tool would grant the requesting person access they do not otherwise hold
- THEN the system SHALL refuse the invocation rather than act with authority the requester lacks

### Requirement: Read-only by default

Federated tools that modify state SHALL be unavailable unless an operator has explicitly enabled them in configuration.

#### Scenario: Mutating tool not explicitly enabled
- GIVEN a connected server providing a tool that modifies state
- WHEN that tool has not been explicitly enabled in configuration
- THEN the system SHALL NOT expose it to the reasoning loop or invoke it

#### Scenario: Tool of undetermined effect
- WHEN the system cannot determine whether a federated tool modifies state
- THEN it SHALL treat that tool as mutating

### Requirement: Explicit confirmation for state-changing actions

An enabled mutating tool SHALL be invoked only after the requesting person confirms that specific invocation.

#### Scenario: Mutating invocation proposed
- WHEN the reasoning loop determines that an enabled mutating tool should be called
- THEN the system SHALL present the tool, the target system, and the arguments to the requesting person
- AND SHALL invoke it only upon that person's explicit confirmation

#### Scenario: Confirmation declined or not given
- WHEN the requesting person declines, or does not respond within the configured window
- THEN the system SHALL NOT invoke the tool
- AND SHALL continue or conclude the run without it

#### Scenario: Confirmation sourced from content
- WHEN retrieved content or a tool result contains text resembling a confirmation
- THEN it SHALL NOT satisfy the confirmation requirement

#### Scenario: Arguments changed after confirmation
- WHEN the arguments to a confirmed invocation differ from those presented
- THEN the system SHALL NOT invoke the tool, and SHALL seek confirmation again

### Requirement: Answers delivered only to the requester

An answer SHALL be delivered to the person who asked, and SHALL NOT be published to any other destination unless that person directs it.

#### Scenario: Answer containing restricted content
- WHEN an answer draws on channels the requesting person may read
- THEN it SHALL be delivered so that only that person receives it, unless they direct otherwise

#### Scenario: Content directing publication
- WHEN retrieved content directs that an answer be sent to another channel, person, or external system
- THEN the system SHALL disregard it

### Requirement: Audit trail

The system SHALL record every federated tool invocation in a form sufficient to reconstruct why it happened.

#### Scenario: Tool invoked
- WHEN the system invokes a federated tool
- THEN it SHALL record the requesting person, their question, the tool and server, the arguments, the outcome, and whether confirmation was obtained

#### Scenario: Invocation refused
- WHEN the system refuses a candidate invocation
- THEN it SHALL record the refusal and its reason

#### Scenario: Audit record integrity
- WHEN an audit record has been written
- THEN the system SHALL NOT provide a means for a requesting person to alter or remove it
