## Purpose

Say what this deployment can actually do, from what it is running, in the
language the question was asked in.

## ADDED Requirements

### Requirement: Capabilities come from configuration, never from the corpus

The system SHALL answer a question about itself from what it is running, and
SHALL NOT search channel content for it.

#### Scenario: Asked what it can do
- WHEN someone asks what the assistant can do
- THEN the answer SHALL be assembled from the running configuration
- AND the corpus SHALL NOT be searched

#### Scenario: A channel describes another product
- WHEN a channel contains a message describing some other tool's features
- THEN those features SHALL NOT be reported as the assistant's own

### Requirement: Every command is listed

The reply SHALL name each command the assistant offers and what it does.

#### Scenario: Asked what it can do
- WHEN someone asks what the assistant can do or which commands exist
- THEN every available command SHALL be listed

#### Scenario: A command that is not available
- WHEN a command's feature is switched off for this deployment
- THEN that command SHALL NOT be listed

### Requirement: Capabilities are named in human terms

The reply SHALL describe what a capability does rather than naming the server
that provides it.

#### Scenario: An external source is registered
- WHEN an external source is registered
- THEN the reply SHALL say what it is for
- AND SHALL NOT present an internal identifier as the capability's name

### Requirement: The reply is in the language of the question

The reply SHALL be written in the language the question was asked in.

#### Scenario: Asked in Portuguese
- WHEN someone asks in Portuguese what the assistant can do
- THEN the reply SHALL be in Portuguese

#### Scenario: Asked in English
- WHEN someone asks in English what the assistant can do
- THEN the reply SHALL be in English
