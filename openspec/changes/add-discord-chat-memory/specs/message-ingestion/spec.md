## Purpose

Capture Discord conversation into a durable, queryable corpus that stays faithful to the source over time — including when messages are edited or deleted — and resolve authors to a platform-independent identity so later platforms map onto the same people.

## ADDED Requirements

### Requirement: Live message capture

The system SHALL record every message posted in an indexed channel, retaining its content, author, channel, timestamp, reply parent, and thread association.

#### Scenario: Message posted in an indexed channel
- WHEN a user posts a message in a channel that is in scope for indexing
- THEN the system SHALL persist the message with its author, channel, timestamp, and content
- AND the message SHALL become retrievable to viewers permitted to read that channel

#### Scenario: Message posted in a channel outside indexing scope
- WHEN a user posts a message in a channel that is not in scope for indexing
- THEN the system SHALL NOT persist the message content

#### Scenario: Connection interrupted
- WHEN the connection to Discord drops and is later re-established
- THEN the system SHALL recover messages posted during the outage
- AND SHALL NOT create duplicate records for messages already persisted

### Requirement: Resumable historical backfill

The system SHALL import messages posted before it was installed, and SHALL record per-channel progress so an interrupted import resumes without gaps or duplicates.

#### Scenario: Backfill of a channel with existing history
- WHEN a backfill is started for a channel containing prior messages
- THEN the system SHALL persist those messages in chronological order
- AND SHALL record a per-channel watermark identifying how far back it has imported

#### Scenario: Backfill interrupted and restarted
- GIVEN a backfill was interrupted partway through a channel
- WHEN the backfill is restarted for that channel
- THEN the system SHALL resume from the recorded watermark
- AND the resulting corpus SHALL contain each message exactly once

#### Scenario: Platform rate limit encountered
- WHEN the platform signals that a rate limit has been exceeded
- THEN the system SHALL wait for the interval indicated by the platform before retrying
- AND SHALL NOT lose its place in the backfill

### Requirement: Edit propagation

The system SHALL reflect message edits so retrieval returns current content rather than superseded content.

#### Scenario: Message edited after indexing
- GIVEN a message has been persisted and indexed
- WHEN its author edits the message
- THEN the system SHALL update the stored content to the edited text
- AND subsequent retrieval SHALL return the edited text and never the superseded text

### Requirement: Deletion tombstoning

The system SHALL stop returning content that has been deleted at the source, so that retracted messages cannot resurface through retrieval.

#### Scenario: Message deleted after indexing
- GIVEN a message has been persisted and indexed
- WHEN the message is deleted in Discord
- THEN the system SHALL mark the message deleted
- AND SHALL NOT return its content, or any excerpt of it, in any subsequent retrieval result

#### Scenario: Deletion while the system is offline
- GIVEN a message was deleted while the system was not running
- WHEN the system next reconciles that channel
- THEN the system SHALL mark the message deleted

### Requirement: Canonical identity resolution

The system SHALL represent each person independently of the platform account they used, so that a single person referenced from multiple platforms resolves to one identity.

#### Scenario: Message from a previously unseen account
- WHEN a message is ingested from a platform account with no existing identity
- THEN the system SHALL create a canonical person and associate the platform account with it

#### Scenario: Message from a known account
- WHEN a message is ingested from a platform account already associated with a canonical person
- THEN the system SHALL attribute the message to that existing person and SHALL NOT create a duplicate

#### Scenario: Display name changed
- WHEN a person changes their display name on the platform
- THEN their previously ingested messages SHALL remain attributed to the same canonical person

### Requirement: Mention capture

The system SHALL record which people are mentioned in a message, so that messages directed at a specific person can be identified without scanning message text.

#### Scenario: Message mentioning a person
- WHEN an ingested message mentions one or more people
- THEN the system SHALL record an association between the message and each mentioned person

### Requirement: Ingestion scope control

The system SHALL allow operators to configure which channels are indexed, and SHALL support removing a channel's content from the corpus.

#### Scenario: Channel removed from indexing scope
- GIVEN a channel's messages have been indexed
- WHEN an operator removes that channel from indexing scope
- THEN the system SHALL stop ingesting new messages from it
- AND SHALL NOT return its previously indexed content in any subsequent retrieval result
