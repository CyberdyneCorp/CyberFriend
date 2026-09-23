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

#### Scenario: An address the asker typed a moment ago
- WHEN a request names no address and one of the asker's own last few
  questions did
- THEN that address SHALL be the one proposed, recorded as typed

#### Scenario: No address at all
- WHEN a request names no address and the asker has saved no wallet
- THEN the reply SHALL ask for one
- AND nothing SHALL be read from a chain

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
- AND the reply SHALL state the bounds, in the asker's language

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
not liquidation protection. Every message SHALL name the commands that list
and stop alerts.

#### Scenario: A Portuguese alert
- WHEN an alert created in Portuguese fires
- THEN the message SHALL be in Portuguese

#### Scenario: The way out is in the message
- WHEN an alert fires
- THEN the message SHALL name `/alert list` and `/alert delete` with its number

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

### Requirement: An alert is asked for in words and created only on the asker's confirmation

The system SHALL recognise a request for a range or health-factor alert in
English or Portuguese, with no model call, before retrieval and before the
positions and wallet routes, under the route label `ALERT_CREATE`. It SHALL
answer with exactly what would be watched and Confirm and Cancel controls, and
SHALL NOT store an alert until the person who asked presses Confirm.

#### Scenario: A request in Portuguese with a saved wallet
- WHEN a person with a saved wallet writes "me avisa se o health factor cair
  abaixo de 1,3"
- THEN the reply SHALL be in Portuguese and list each chain with Aave debt,
  the limit and the health factor now
- AND the archive SHALL NOT be searched and no model SHALL be called
- AND no alert SHALL exist until they press Confirm

#### Scenario: Confirm
- WHEN the person who asked presses Confirm
- THEN one alert per listed target SHALL be created, skipping any already
  watched and any past the cap
- AND the reply SHALL say what was created, by number

#### Scenario: Cancel, or nothing
- WHEN the person presses Cancel, or presses nothing for five minutes
- THEN no alert SHALL be created and the controls SHALL be removed

#### Scenario: Somebody else presses
- WHEN anybody but the person who asked presses Confirm or Cancel
- THEN nothing SHALL be created or cancelled
- AND they SHALL be told privately that it is not theirs to confirm

#### Scenario: A question about alerts
- WHEN a message asks what people said about alerts, asks for a reading
  ("tell me if my health factor is ok"), or asks about alerting itself
  ("does Uniswap notify me when my position goes out of range?", "como criar
  um alerta quando a posição sair da faixa?")
- THEN it SHALL NOT be treated as an alert request
- AND a request put politely to the assistant ("can you alert me when my LP
  goes out of range?") SHALL still be one

#### Scenario: Asked by a scheduled question
- WHEN a scheduled question is an alert request
- THEN it SHALL be answered as any other question, since nobody is present to
  confirm a proposal
- AND no chain SHALL be read for a proposal and no "not available" reply SHALL
  be sent on its behalf

#### Scenario: Alerts not enabled
- WHEN the deployment has not enabled alerts, or has no chain endpoint key,
  and a person asks for one
- THEN the reply SHALL say alerts are not available here, in their language
- AND the archive SHALL NOT be searched

### Requirement: The confirmation shows the baseline and what it leaves out

The confirmation SHALL show, for each target, the reading taken when it was
asked for; SHALL say when a target is already past its condition; and SHALL
name what it leaves out: chains that could not be read, positions that could
not be listed, targets already watched, targets past the cap, and positions
opened later.

#### Scenario: A range already out
- WHEN a listed position is out of range when the request is made
- THEN the confirmation SHALL say so, and that the next message comes when it
  is back in range

#### Scenario: A chain that could not be read
- WHEN a chain cannot be read while proposing
- THEN the confirmation SHALL name it rather than treat it as holding nothing

#### Scenario: No debt, or no positions
- WHEN the read finds no open position, or no Aave debt, to watch
- THEN the reply SHALL say so and offer nothing to confirm

### Requirement: The wallet is named only to its owner

A confirmation or its result SHALL name the watched address only in a direct
message with the person who asked. In a channel, including an ephemeral reply
there, it SHALL describe the targets without the address.

#### Scenario: Asked in a channel
- WHEN a person mentions the assistant in a channel with an alert request
- THEN the confirmation SHALL be posted there without the wallet address
- AND only that person SHALL be able to press it

### Requirement: A person lists and stops their alerts with commands

`/alert list` and `/alert delete` SHALL be available in the server and in a
direct message with the assistant, SHALL act on the invoking account's alerts
only, and SHALL answer privately in the language of the person's client. The
capability reply SHALL list them where alerts are enabled.

#### Scenario: Listing
- WHEN a person runs `/alert list`
- THEN each of their alerts SHALL be shown with what it watches, its state and
  since when, and whether it is being checked, failing or stopped

#### Scenario: Deleting
- WHEN a person runs `/alert delete` with the number of one of their alerts
- THEN it SHALL stop being watched
