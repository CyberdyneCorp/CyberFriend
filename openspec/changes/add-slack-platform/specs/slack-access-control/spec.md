## ADDED Requirements

### Requirement: A Slack person reads only what Slack lets them read

The Slack ACL resolver SHALL grant an indexed channel to a person only when
Slack would let that person read it: a full member of the workspace reads
every indexed public channel of that workspace and the indexed private
channels they are a member of; a multi-channel or single-channel guest reads
only indexed channels they are a member of; an external Slack Connect user
reads only indexed shared channels they are a member of. A deactivated,
unknown or bot user SHALL read nothing.

#### Scenario: A full member and a public channel they never joined
- WHEN a full member asks about an indexed public channel they are not in
- THEN evidence from that channel SHALL be available to them

#### Scenario: A guest and a public channel
- WHEN a multi-channel guest asks about an indexed public channel they are not
  a member of
- THEN no evidence from that channel SHALL be returned

#### Scenario: A private channel
- WHEN a person who is not a member of an indexed private channel asks about it
- THEN no evidence from it SHALL be returned, at the repository layer

#### Scenario: An external Slack Connect user
- WHEN a user from another organisation, present in one shared channel, asks a
  question
- THEN only evidence from that shared channel SHALL be available

#### Scenario: A deactivated user
- WHEN a deactivated user's identity is used to query, including through an
  MCP token
- THEN no evidence SHALL be returned

### Requirement: Slack membership is kept current and lookups fail closed

Channel membership and user status SHALL be cached from Slack and kept
current by membership, channel and user events, with a periodic refresh. Any
error resolving a person's status or a channel's membership SHALL yield no
access to the affected channel.

#### Scenario: A person leaves a private channel
- WHEN a `member_left_channel` event arrives for a private channel
- THEN the person SHALL lose access to its evidence from their next question

#### Scenario: A channel made private or shared externally
- WHEN a channel's privacy or external sharing changes
- THEN its readers SHALL be recomputed before the next answer that could cite
  it

#### Scenario: Slack unreachable
- WHEN membership for a channel cannot be fetched and the cache has expired
- THEN the channel SHALL be treated as unreadable by everyone until it can be

### Requirement: Channel replies draw only on what the whole audience can read

An answer posted visibly in a Slack conversation SHALL use evidence only from
channels every member of that conversation can read, including anyone who
could open it later. In a public channel without guests or external members,
the audience SHALL read only the indexed public channels of that workspace,
never a private channel, whoever the current members are. In a public channel
with guests or external members, and in a Slack Connect channel, the audience
SHALL read only that channel. A thread SHALL take its parent channel's
audience.

#### Scenario: A question in a Slack Connect channel
- WHEN someone asks in a shared channel about another indexed channel
- THEN the visible answer SHALL NOT use evidence from any other channel
- AND the asker SHALL be offered a private answer instead

#### Scenario: A public channel with a guest present
- WHEN a guest is a member of a public channel where a question is asked
- THEN the visible answer SHALL use evidence only from that channel

#### Scenario: A public channel whose members all share a private channel
- WHEN a question is asked in a public channel whose three members all belong
  to the private channel #exec
- THEN the visible answer SHALL NOT use #exec evidence
- AND the asker SHALL be offered a private answer instead

#### Scenario: A private channel
- WHEN a question is asked in a private channel
- THEN the visible answer SHALL use only channels every member of that private
  channel can read

### Requirement: Only a direct message with the bot is private on Slack

A Slack `im` between one person and the bot SHALL be the only private
conversation. An `mpim` SHALL be treated as a group conversation, never as
private. DM-only personal facts SHALL be shown only in an `im` or an ephemeral
reply to their owner.

#### Scenario: Asking for one's birth date in an mpim
- WHEN a person asks for their saved birth date in a group DM that includes
  the bot
- THEN the reply SHALL withhold it and say it is available in a direct
  message

#### Scenario: The same question in a DM
- WHEN the person asks in their `im` with the bot
- THEN the fact SHALL be shown

### Requirement: Enterprise Grid access is resolved per workspace

On an Enterprise Grid install, a person's readable set SHALL be computed from
the workspaces they belong to, and public channels SHALL count only for those
workspaces.

#### Scenario: A public channel in a workspace the person is not in
- WHEN a Grid member asks about an indexed public channel in a workspace of
  the org they do not belong to
- THEN no evidence from it SHALL be returned
