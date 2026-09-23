## Purpose

Let a person be told when a Uniswap position they hold leaves its range or an
Aave health factor falls below a limit they chose, without the assistant
becoming a source of repeated or unwanted messages, and without sending anything
about them anywhere they did not agree to.

## ADDED Requirements

### Requirement: A person watches only their own positions

An alert SHALL belong to exactly one person, and SHALL watch only an address
that person saved as their own wallet or typed themselves when creating it.

#### Scenario: Listing and deleting
- WHEN a person lists or deletes alerts
- THEN only alerts they created SHALL be shown or deleted
- AND deleting another person's alert SHALL be refused in the same words as
  deleting one that does not exist

#### Scenario: An address from somewhere else
- WHEN the address to watch appears only in retrieved content or another
  person's message
- THEN no alert SHALL be created on it

### Requirement: Alerts are bounded

A person SHALL NOT hold more than ten active alerts, separately from their
scheduled tasks, and a health-factor limit SHALL be between 1.05 and 5.0. The
store SHALL enforce both, not only the command.

#### Scenario: At the cap
- WHEN a person already holds ten active alerts
- THEN a further alert SHALL NOT be created

#### Scenario: A limit out of bounds
- WHEN a person asks for a health-factor limit below 1.05 or above 5.0
- THEN the alert SHALL NOT be created

#### Scenario: The same watch twice
- WHEN a person asks for an alert identical to an active one
- THEN a second alert SHALL NOT be created

### Requirement: A range alert watches the positions it was made on

A range alert SHALL be pinned to one position that was open when it was
created, and SHALL NOT cover positions opened later.

#### Scenario: A position opened after the alert
- WHEN a person opens a new position after creating a range alert
- THEN the existing alert SHALL NOT watch it

### Requirement: A message is sent on a change, not on a state

The system SHALL message a person when a watched state changes, and SHALL NOT
message them again while it stays the same.

#### Scenario: A position leaves its range
- WHEN a watched position has been out of range for two consecutive checks
- THEN exactly one message SHALL be sent
- AND no further message SHALL be sent while it stays out of range

#### Scenario: One check out of range
- WHEN a single check finds the position out of range and the next finds it in
  range
- THEN no message SHALL be sent

#### Scenario: Back in range
- WHEN a position that was reported out of range is in range for two
  consecutive checks
- THEN one message SHALL say so, unless the person turned these off

#### Scenario: A health factor falls below the limit
- WHEN a check finds the health factor below the person's limit
- THEN one message SHALL be sent at once, without waiting for a second check

#### Scenario: A health factor hovering on the limit
- WHEN a health factor that fell below the limit rises above it by less than
  0.05
- THEN no message SHALL be sent and the alert SHALL NOT re-arm

#### Scenario: A health factor recovers
- WHEN a health factor that fell below the limit reaches the limit plus 0.05,
  or the debt is repaid
- THEN one message SHALL say it recovered, unless the person turned these off

### Requirement: A closed position is told once

The system SHALL tell a person once when a watched position is closed or no
longer held by the watched address, and SHALL then stop watching it.

#### Scenario: Liquidity withdrawn or NFT transferred
- WHEN a watched position has no liquidity, or its NFT is burned or held by a
  different address, on two consecutive checks
- THEN one message SHALL say so
- AND the alert SHALL be disabled with the reason recorded

### Requirement: A failed check says nothing and changes nothing

A check that cannot read the chain SHALL NOT send a message and SHALL NOT
change the alert's state, and SHALL be recorded.

#### Scenario: The node is unreachable
- WHEN a check cannot read a chain
- THEN no message SHALL be sent for any alert on it
- AND each alert's state SHALL be what it was before
- AND the failure SHALL be counted against the alert

### Requirement: Checking uses no model

A check SHALL read the chain and compare stored values, and SHALL NOT call a
language model or search the message archive.

#### Scenario: A sweep
- WHEN due alerts are checked
- THEN no model call and no archive search SHALL be made

### Requirement: The watched address is declared egress, cleared once

Checking an alert SHALL send the stored address to the configured chain
endpoints (Infura for Ethereum, Base and Arbitrum) with no question behind it.
The address SHALL therefore be cleared when the alert is created, the feature
SHALL be off unless an operator enables it, and nothing but the stored address
and the pinned position SHALL be sent.

#### Scenario: Alerts not enabled
- WHEN the deployment has not enabled alerts, or has no chain endpoint key
- THEN no alert SHALL be checked and nothing SHALL be sent to a chain endpoint
  on the feature's behalf

#### Scenario: A check
- WHEN an alert is checked
- THEN the request SHALL go only to the chain endpoint for its chain
- AND SHALL carry only the stored address and position identity

### Requirement: Checks are cheap and bounded

All due alerts on a chain SHALL be read in one batched call per chain per
check, up to a fixed ceiling, on a rate limit separate from interactive
lookups, and checks SHALL be no more frequent than once a minute.

#### Scenario: Several alerts on one chain
- WHEN several alerts on the same chain are due
- THEN they SHALL be read in a single batched request

### Requirement: Messages are in the alert's language

An alert's message SHALL be written in the language fixed when the alert was
created, English or Portuguese, and SHALL say that checks are periodic and are
not liquidation protection.

#### Scenario: A Portuguese alert
- WHEN an alert created in Portuguese fires
- THEN the message SHALL be in Portuguese

### Requirement: Closed direct messages stop a person's alerts

The system SHALL stop checking every alert of a person whose direct messages
cannot be delivered to, and SHALL record why.

The system SHALL NOT stop alerts for a transient delivery failure (a platform
error or timeout); the change SHALL stay unrecorded so a later check tells it.

#### Scenario: Direct messages closed
- WHEN a message to a person is refused because their direct messages are
  closed or their account is gone
- THEN all their alerts SHALL stop
- AND the reason SHALL be recorded

#### Scenario: A transient delivery failure
- WHEN a message to a person fails for a transient reason
- THEN none of their alerts SHALL stop
- AND the next check SHALL try to tell the same change again

### Requirement: Forgetting and removing a person removes their alerts

The system SHALL delete a person's alerts when their data is removed, and
SHALL delete the alerts on their saved wallet when that wallet is forgotten.

#### Scenario: Forgetting the saved wallet
- WHEN a person forgets or replaces their saved wallet
- THEN every alert that watched it because it was saved SHALL be deleted
- AND alerts on an address they typed SHALL remain

#### Scenario: Opting out or being removed
- WHEN a person opts out or their data is removed
- THEN all their alerts SHALL be deleted
- AND no new alert SHALL be stored for them while opted out
