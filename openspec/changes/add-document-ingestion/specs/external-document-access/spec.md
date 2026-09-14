## Purpose

Follow links to documents held in external systems without letting the bot's access to those systems become a way for anyone in Discord to read things they could not otherwise reach.

## ADDED Requirements

### Requirement: Only configured sources are followed

The system SHALL follow links only to external systems named in its configuration, and SHALL NOT fetch arbitrary URLs.

#### Scenario: Link to a configured system
- WHEN a message in an indexed channel links to a document in a configured system
- THEN the system MAY retrieve and index that document

#### Scenario: Link to any other destination
- WHEN a message links to a destination that is not a configured system
- THEN the system SHALL NOT fetch it

#### Scenario: Instruction to fetch a destination
- WHEN a message, document, or tool result instructs the system to fetch a destination
- THEN that SHALL NOT cause a fetch, regardless of configuration

#### Scenario: External fetching disabled
- WHEN external fetching is disabled in configuration
- THEN the system SHALL fetch no external documents at all

### Requirement: External credentials are bounded

The credentials used to fetch external documents SHALL grant no more access than is already available to the people whose channels the links appear in.

#### Scenario: Document the bot can read but the team cannot
- WHEN the configured credential can read a document that is not shared with the team
- THEN the configuration SHALL be rejected, or such documents SHALL NOT be retrievable

#### Scenario: Fetch attempt beyond the credential's access
- WHEN a linked document cannot be read with the configured credential
- THEN the system SHALL record that it could not be retrieved
- AND SHALL NOT report the document's existence or any part of its content

### Requirement: Visibility comes from Discord, not the external system

An external document's visibility SHALL be that of the Discord channel where it was linked, and SHALL NOT be derived from the external system's own permissions.

#### Scenario: Document linked in a private channel
- GIVEN a document linked in a channel a viewer may not read
- WHEN that viewer issues a query its content would match
- THEN it SHALL NOT appear in the results, even if that viewer could open the document directly

#### Scenario: Document linked in two channels
- WHEN a document is linked from more than one channel
- THEN it SHALL be readable by a viewer permitted to read any one of them

#### Scenario: External permissions change
- WHEN a document's permissions change in the external system
- THEN its visibility within the corpus SHALL remain that of the channels it was linked in

### Requirement: External content is refreshed and can be withdrawn

The system SHALL keep indexed external content reconcilable with its source, and SHALL stop returning content that is no longer retrievable.

#### Scenario: Document changes at the source
- WHEN a linked document's content changes
- THEN the system SHALL update the indexed content on its next reconciliation
- AND retrieval SHALL return the current content rather than the superseded content

#### Scenario: Document deleted or access withdrawn at the source
- WHEN a previously indexed document can no longer be retrieved
- THEN the system SHALL stop returning its content

#### Scenario: Linking message deleted
- WHEN the message that linked a document is deleted
- THEN the system SHALL stop returning that document's content, unless another remaining message also links it

### Requirement: External fetching is bounded and observable

Fetching SHALL operate under limits that prevent one document, or one link, from consuming the system, and SHALL be recorded.

#### Scenario: Fetch exceeds its time or size limit
- WHEN a fetch exceeds the configured time or size limit
- THEN the system SHALL abandon it and record the failure

#### Scenario: External system unavailable
- WHEN a configured system is unavailable
- THEN ingestion of messages and attachments SHALL continue unaffected

#### Scenario: Document fetched
- WHEN the system retrieves an external document
- THEN it SHALL record what was fetched, from where, and which message linked it
