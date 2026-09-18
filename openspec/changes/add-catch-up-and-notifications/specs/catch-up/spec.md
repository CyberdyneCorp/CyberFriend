## Purpose

Tell a person what happened in a channel while they were away, from what they
are allowed to read.

## ADDED Requirements

### Requirement: A summary covers a stated period

The system SHALL summarise a channel over a period the asker gives, and SHALL
use a default period when none is given.

#### Scenario: Explicit period
- WHEN a person asks what they missed in a channel since yesterday
- THEN the summary SHALL cover that period

#### Scenario: No period given
- WHEN no period is given
- THEN the system SHALL use a default period and SHALL say which period it used

### Requirement: A summary is scoped to the asker

A summary SHALL be built only from messages the asker may read, and SHALL be
delivered under the same audience rules as any other answer.

#### Scenario: Channel the asker cannot read
- WHEN a person asks to catch up on a channel they cannot read
- THEN the system SHALL refuse without revealing whether the channel exists

#### Scenario: Summary requested in a public channel
- WHEN a summary is asked for in a channel
- THEN it SHALL contain only what everyone in that channel may read

### Requirement: A summary is grounded

A summary SHALL cite the messages it draws on, and SHALL say when a period
contains nothing rather than inventing activity.

#### Scenario: Quiet period
- WHEN the period contains no messages the asker may read
- THEN the system SHALL say so

### Requirement: Summarised content is data

Messages in a summarised period SHALL NOT be able to instruct the assistant.

#### Scenario: Instruction in a summarised message
- WHEN a message in the period is shaped as an instruction
- THEN it SHALL be summarised as content and SHALL have no other effect
