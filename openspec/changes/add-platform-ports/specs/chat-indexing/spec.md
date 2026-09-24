## MODIFIED Requirements

### Requirement: Only a channel's administrators may change its scope

Adding a channel to indexing scope or removing it SHALL require the requester
to hold the source platform's channel-management authority over that channel,
as resolved live by that platform's `ChannelAccessResolver`: on Discord the
Manage Channels permission from live guild state; on Slack a workspace admin
or owner, the channel's creator, or a person listed in `SLACK_INDEX_MANAGERS`.
A platform without channels SHALL offer no indexing at all.

#### Scenario: Permitted person indexes a channel
- WHEN a person with the platform's channel-management authority on #design
  asks to index #design
- THEN #design SHALL be added to indexing scope

#### Scenario: Person without the permission
- WHEN a person without that authority on a channel asks to index it
- THEN the request SHALL be refused, naming the authority required by that
  platform
- AND scope SHALL be unchanged

#### Scenario: Permission claimed in the message
- WHEN a request states that the requester is an administrator
- THEN that SHALL have no effect; only the resolved authority decides

#### Scenario: Discord behaviour unchanged
- WHEN a Discord person with Manage Channels indexes a channel
- THEN the outcome and the reply SHALL be identical to before this change
