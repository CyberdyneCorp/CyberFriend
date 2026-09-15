## Purpose

Hold the settings an operator may change in the database rather than the
environment, so a change applies to a running system, carries a record of who
made it, and can be explained by showing where the value came from.

## ADDED Requirements

### Requirement: Stored configuration takes precedence over the environment

A setting present in the database SHALL be used in place of the environment
value, and a setting absent from the database SHALL fall back to it.

#### Scenario: Setting stored
- WHEN a setting has a stored value
- THEN the system SHALL use it

#### Scenario: Setting not stored
- WHEN a setting has no stored value
- THEN the system SHALL use the environment value, or its default

#### Scenario: Explaining a value
- WHEN an operator views a setting
- THEN the system SHALL report whether it came from the database, the
  environment, or a default

### Requirement: Secrets are environment-only

Credentials SHALL be read from the environment and SHALL NOT be storable.

#### Scenario: Storing a credential
- WHEN a credential is written to stored configuration
- THEN the system SHALL refuse it

#### Scenario: Credential absent from the environment
- WHEN a required credential is missing from the environment
- THEN the system SHALL fail to start and name what is missing

### Requirement: A change reaches a running process

A stored change SHALL take effect without redeploying, within a bounded period.

#### Scenario: Setting changed while running
- WHEN a setting is changed
- THEN each running process SHALL apply it within its configured refresh period

#### Scenario: Narrowing what is reachable
- WHEN a channel is removed from indexing scope, or a tool from the allowlist
- THEN processes SHALL stop using it within that period
- AND SHALL NOT wait for a restart

#### Scenario: Database unavailable at refresh
- WHEN stored configuration cannot be read during a refresh
- THEN the system SHALL continue with the configuration it already has
- AND SHALL NOT fall back to the environment, which would silently undo a
  narrowing an operator has already made

### Requirement: Invalid stored configuration does not stop a running process

Configuration that cannot be applied SHALL be reported and skipped rather than
taking down a process that is working.

#### Scenario: Stored value cannot be parsed
- WHEN a stored setting is malformed
- THEN the system SHALL keep its previous value and record the problem

#### Scenario: Invalid at startup
- WHEN stored configuration is invalid as a process starts
- THEN the process SHALL start with the environment configuration and report
  what it could not apply
