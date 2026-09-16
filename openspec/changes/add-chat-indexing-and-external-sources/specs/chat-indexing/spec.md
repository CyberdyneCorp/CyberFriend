## Purpose

Let the people who administer a channel decide, from Discord, whether the
assistant archives it -- and make sure everyone in that channel knows.

## ADDED Requirements

### Requirement: Only a channel's administrators may change its scope

Adding a channel to indexing scope or removing it SHALL require the requester to
hold Discord's Manage Channels permission on that channel, as resolved from live
guild state.

#### Scenario: Permitted person indexes a channel
- WHEN a person with Manage Channels on #design asks to index #design
- THEN #design SHALL be added to indexing scope

#### Scenario: Person without the permission
- WHEN a person without Manage Channels on a channel asks to index it
- THEN the request SHALL be refused
- AND scope SHALL be unchanged

#### Scenario: Permission claimed in the message
- WHEN a request states that the requester is an administrator
- THEN that SHALL have no effect; only the resolved permission decides

### Requirement: The assistant must be able to read the channel

The system SHALL refuse to index a channel the assistant cannot read, and SHALL
say why.

#### Scenario: Bot lacks read access
- WHEN a channel is requested that the bot cannot view or read history in
- THEN the request SHALL be refused with the missing permission named

### Requirement: The channel is told

When a channel is added to indexing scope, the assistant SHALL post a notice in
that channel saying it is now being archived and how to ask for removal.

#### Scenario: Channel indexed
- WHEN a channel is added to scope
- THEN a notice SHALL be posted in that channel

### Requirement: Removal withdraws content

Removing a channel from indexing scope SHALL withdraw its content from retrieval,
not only stop new capture.

#### Scenario: Channel removed
- WHEN a channel is removed from scope
- THEN its messages and windows SHALL no longer be returned to anyone

### Requirement: Changes are recorded

Every scope change made from Discord SHALL be recorded with the requester, the
channel, and whether it was allowed.

#### Scenario: Refused request
- WHEN an indexing request is refused
- THEN the attempt SHALL be recorded with the reason
