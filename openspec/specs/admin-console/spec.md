# admin-console Specification

## Purpose
Let an operator change what the agent may reach and what it archives, with every
change attributable to a person and recorded, and without ever exposing the
corpus or the credentials the agent runs on.
## Requirements
### Requirement: Operator identity comes from the credential

Every request SHALL be authenticated, and the credential SHALL identify which
operator is acting. A credential that grants access without naming who holds it
SHALL NOT be accepted.

#### Scenario: Request with a valid credential
- WHEN a request arrives with a credential issued to an operator
- THEN the system SHALL act as that operator
- AND SHALL record that operator against anything the request changes

#### Scenario: Request without a credential
- WHEN a request arrives with no credential, or one that is unknown, malformed
  or revoked
- THEN the system SHALL refuse it
- AND the refusal SHALL be indistinguishable between those cases

#### Scenario: Console served under a path prefix
- WHEN the console is served behind a path prefix, so that the request path
  carries the prefix and the router resolves routes without it
- THEN any request the router dispatches to a console route SHALL be
  authenticated first
- AND the prefix SHALL NOT make any route reachable without a credential

#### Scenario: Operator named in the request
- WHEN a request states which operator it is acting as
- THEN that SHALL have no effect; only the credential decides

#### Scenario: Revoking one operator
- WHEN an operator's credential is revoked
- THEN their subsequent requests SHALL be refused
- AND every other operator's credential SHALL continue to work

### Requirement: Every change is recorded

The system SHALL record each configuration change with the operator, the time,
what was changed, and the values before and after.

#### Scenario: Setting changed
- WHEN an operator changes a setting
- THEN the system SHALL record the operator, the setting, its previous value
  and its new value

#### Scenario: Change rejected
- WHEN a change is refused
- THEN the system SHALL record the attempt and the reason

#### Scenario: Refused value the system did not recognise
- WHEN a refused change names a server, tool or setting value that the system
  does not already hold
- THEN neither the refusal nor the record SHALL repeat that value
- AND the record SHALL still name the setting, the operator and the reason

#### Scenario: Record is append-only
- WHEN a record has been written
- THEN the console SHALL provide no means of altering or removing it

### Requirement: Secrets are never exposed

The console SHALL NOT display, return, or accept the credentials the agent runs
on.

#### Scenario: Viewing configuration
- WHEN an operator views configuration
- THEN no platform token, model key or database URL SHALL appear in the
  response, in any form, including partially masked

#### Scenario: Attempting to set a secret
- WHEN a request tries to set one of those values
- THEN the system SHALL refuse it

### Requirement: The console cannot grant access to the corpus

The console SHALL NOT create a credential that reads the corpus. Credentials
that bind a viewer are issued outside the console, from a shell holding the
agent's own environment.

#### Scenario: Requesting a new corpus credential
- WHEN a request asks the console to issue a credential for a platform account
- THEN the system SHALL refuse it
- AND no credential SHALL be created

#### Scenario: Reviewing and withdrawing
- WHEN an operator reviews issued credentials
- THEN the system SHALL report the person, label and timestamps and SHALL NOT
  return the credential or its stored hash
- AND the operator SHALL be able to revoke one, which only narrows access

### Requirement: The corpus is not reachable through the console

The console SHALL configure the agent and SHALL NOT expose message, document or
ask content.

#### Scenario: Requesting content
- WHEN a request asks for message, document or ask content
- THEN the system SHALL refuse it

#### Scenario: Diagnostics
- WHEN the console reports ingestion progress
- THEN it MAY report counts and timings
- AND SHALL NOT include content or excerpts

### Requirement: Enabling a state-changing tool is deliberate

Enabling a federated tool that modifies state SHALL require a confirmation
naming that specific tool, and SHALL be recorded distinctly from other changes.

#### Scenario: Enabling a mutating tool
- WHEN an operator enables a tool that modifies state
- THEN the system SHALL require a confirmation identifying that tool by name
- AND SHALL record the change as an escalation of what the agent may do

#### Scenario: Confirmation naming a different tool
- WHEN the confirmation does not match the tool being enabled
- THEN the system SHALL refuse the change

#### Scenario: Effect not declared
- WHEN a tool is added without a declared effect
- THEN it SHALL be treated as state-changing

#### Scenario: Reviewing what is enabled
- WHEN an operator views the allowlist
- THEN tools that modify state SHALL be distinguishable from those that do not

### Requirement: Configuration is validated before it takes effect

The system SHALL reject configuration that a running agent could not use, at
the moment it is submitted.

#### Scenario: Server that cannot be reached
- WHEN an operator adds a federated server
- THEN the system SHALL report whether it could be reached and which tools it
  offers

#### Scenario: Allowlisting a tool no server provides
- WHEN a tool is allowlisted that no configured server offers
- THEN the system SHALL refuse the change rather than accept configuration that
  would fail at startup

#### Scenario: Channel the agent cannot read
- WHEN a channel is added to indexing scope that the agent cannot read
- THEN the system SHALL report this rather than accept it silently

