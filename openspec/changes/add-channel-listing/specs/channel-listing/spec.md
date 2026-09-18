## Purpose

Let someone see which channels are archived, without the answer disclosing
channels they cannot read.

## ADDED Requirements

### Requirement: Only channels the asker can read are listed

The system SHALL list an archived channel only when the asker can read that
channel.

#### Scenario: An archived channel the asker can read
- WHEN someone asks which channels are archived
- THEN each archived channel they can read SHALL be listed

#### Scenario: An archived channel the asker cannot read
- WHEN an archived channel is one the asker cannot read
- THEN it SHALL NOT be listed
- AND the reply SHALL NOT disclose that it exists, by name, count or otherwise

#### Scenario: Access resolved empty
- WHEN the asker cannot be resolved to a member of the server
- THEN nothing SHALL be listed

### Requirement: The listing matches what is actually searchable

The set listed SHALL be the archived channels intersected with the asker's
readable channels, resolved from live platform state at the time of asking.

#### Scenario: Access changed since indexing
- WHEN somebody has lost access to a channel since it was archived
- THEN that channel SHALL NOT be listed for them

#### Scenario: Scope changed without a redeploy
- WHEN a channel is archived or un-archived
- THEN the next listing SHALL reflect it

### Requirement: The reply is private

The reply SHALL be visible only to the person who asked.

#### Scenario: Asked in a channel
- WHEN the listing is requested in a channel
- THEN the reply SHALL NOT be visible to others in that channel

### Requirement: Nothing archived reads as nothing archived

The system SHALL distinguish having nothing to show from failing to answer.

#### Scenario: No archived channel is readable by the asker
- WHEN the asker can read none of the archived channels
- THEN the reply SHALL say there are none to show them
- AND SHALL NOT imply that no channel is archived anywhere
