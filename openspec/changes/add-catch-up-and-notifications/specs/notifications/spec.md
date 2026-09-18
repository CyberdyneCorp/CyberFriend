## Purpose

Tell a person when someone has asked them for something, without the assistant
becoming a source of unwanted messages.

## ADDED Requirements

### Requirement: Only the addressee is notified

A notification SHALL be sent only to the person an obligation is addressed to.

#### Scenario: Obligation addressed to a person
- WHEN an obligation addressed to a person is extracted
- THEN that person MAY be notified
- AND no one else SHALL be notified of it

#### Scenario: Obligation addressed to a group
- WHEN an obligation names no individual
- THEN no notification SHALL be sent

### Requirement: Notifications are batched and bounded

The system SHALL combine pending notifications for one person into a single
message and SHALL NOT exceed a configured rate.

#### Scenario: Several obligations in quick succession
- WHEN a person is asked several things within the batching window
- THEN they SHALL receive one message covering all of them

#### Scenario: Rate reached
- WHEN the rate limit is reached
- THEN further notifications SHALL wait rather than being sent or dropped

### Requirement: People can turn notifications off

A person SHALL be able to stop notifications, and the system SHALL send none to
a person who has opted out of indexing.

#### Scenario: Turning them off
- WHEN a person tells the assistant to stop notifying them
- THEN no further notifications SHALL be sent to them

#### Scenario: Opted-out person
- WHEN a person has opted out
- THEN no notification SHALL be sent to them

#### Scenario: First notification
- WHEN the assistant notifies a person for the first time
- THEN the message SHALL say how to stop them

### Requirement: Permission is re-checked at send time

A notification SHALL be sent only if the recipient may still read the channel
the obligation came from.

#### Scenario: Access revoked before sending
- WHEN a person loses access to the channel between extraction and sending
- THEN the notification SHALL NOT be sent

### Requirement: A notification says only what it must

A notification SHALL identify the channel, who asked, and link to the message,
and SHALL NOT quote content beyond a short excerpt.

#### Scenario: Notification content
- WHEN a notification is sent
- THEN it SHALL link to the source message and name its channel

### Requirement: Delivery failure is not retried forever

The system SHALL stop trying to reach a person whose direct messages are closed.

#### Scenario: Direct messages closed
- WHEN a person cannot be sent a direct message
- THEN the system SHALL record it and SHALL NOT retry indefinitely
