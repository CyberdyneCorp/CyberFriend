## ADDED Requirements

### Requirement: Platform identifiers are opaque strings qualified by platform

People, channels, messages and conversations SHALL be identified by a
platform and an opaque string id. The core SHALL NOT parse, compare
numerically or order by an external id, and SHALL NOT assume any platform
when rebuilding a reference from storage.

#### Scenario: A Slack id is representable
- WHEN an adapter reports a person `slack:U0123ABC` in channel `slack:C0456DEF`
- THEN the domain SHALL hold both references without conversion or loss

#### Scenario: The same id on two platforms
- WHEN `discord:123` and `slack:123` both exist
- THEN they SHALL be distinct people, channels and conversation locations
- AND nothing stored for one SHALL be returned for the other

#### Scenario: A row is rebuilt with its own platform
- WHEN a store reads a person, channel, message, ask, alert, schedule or
  notification row
- THEN the reference SHALL carry the platform stored on that row
- AND no store SHALL stamp a constant platform onto it

#### Scenario: A reference to an unregistered platform
- WHEN a request carries a reference whose platform is not enabled in this
  deployment
- THEN every router SHALL refuse it as unavailable
- AND no ACL resolver SHALL grant it any channel

### Requirement: Existing Discord data migrates without changing what anyone can read

The migration SHALL keep every existing Discord row's internal key and every
stored permission array value, SHALL record each existing row's platform as
`discord` and its external id as the decimal snowflake, and SHALL rewrite the
indexed-channel setting to platform-qualified entries.

#### Scenario: Discord rows after the migration
- WHEN the migration runs on a database holding Discord channels, messages,
  windows, asks, facts, alerts, schedules and conversation turns
- THEN every row SHALL still exist with the same internal id
- AND every person, channel and message SHALL resolve to the same Discord
  reference as before
- AND a viewer SHALL retrieve exactly the evidence they retrieved before

#### Scenario: The indexed-channel setting
- WHEN the stored setting lists channels `111 222`
- THEN after the migration it SHALL list `discord:111 discord:222`
- AND the scope SHALL contain the same two channels

#### Scenario: New platforms cannot collide with snowflakes
- WHEN a non-Discord channel or message is stored
- THEN its internal id SHALL come from a descending negative sequence, which
  is disjoint from every Discord snowflake by construction

#### Scenario: Thread and message references on a string-id platform
- WHEN a Slack thread (`thread_ts` string) holds a message, an ask, a reaction
  and a conversation window
- THEN windows, asks, ask reactions and document entries SHALL reference it
  without loss, through TEXT thread ids and internal message ids

#### Scenario: A channel in several workspaces
- WHEN a Slack channel is shared between two workspaces of one Grid org
- THEN the channel SHALL record both workspaces, and the ACL SHALL use them

#### Scenario: Downgrade with foreign rows
- WHEN the downgrade runs while any non-Discord row exists
- THEN it SHALL refuse and change nothing

### Requirement: Ordering and resumption do not depend on id order

Ingest SHALL order messages by posted time and an adapter-supplied sequence
key, and SHALL resume backfill from an opaque cursor the adapter returned, not
from the smallest id seen.

#### Scenario: Resuming a backfill
- WHEN a backfill is interrupted after storing a page and restarted
- THEN it SHALL continue from the stored cursor
- AND no message SHALL be stored twice or skipped

#### Scenario: A platform without history
- WHEN a platform's capabilities declare no history backfill
- THEN ingest SHALL NOT request a backfill for its conversations
- AND live messages SHALL still be stored

### Requirement: A platform id is never shown or logged as a name

When a person has no display name, the system SHALL use a neutral label, not
their platform id, in windows, prompts, stored display names and replies, and
logs SHALL carry only a redacted form of a person reference.

#### Scenario: A person without a display name
- WHEN a message arrives from a person whose platform supplied no name
- THEN the window text and the model prompt SHALL NOT contain their id
- AND the stored person SHALL NOT take their id as display name

#### Scenario: Logging a person
- WHEN a log line concerns a person
- THEN it SHALL contain the redacted reference, not the raw id

### Requirement: Identifiers entered by operators and clients are platform-qualified

Credential holders, MCP tokens, MCP tool parameters and admin-console account
ids SHALL accept `platform:id`. A bare numeric id SHALL keep meaning a Discord
id, so existing configuration and tokens keep working.

#### Scenario: An existing Discord token
- WHEN an MCP token issued before the change is presented
- THEN it SHALL authenticate the same Discord person as before

#### Scenario: A Slack credential holder
- WHEN the operator names `slack:U0123ABC` as a credential holder
- THEN the setting SHALL be accepted and resolve to that Slack person

#### Scenario: A malformed identifier
- WHEN an identifier names an unknown platform or is empty after the prefix
- THEN it SHALL be rejected with an error naming the expected form

### Requirement: Identities on different platforms are linked only by proof

Any two platform identities SHALL be linked to one person only by redeeming a
one-time code that the person obtained in a private conversation on the
issuing platform, and only after the person confirms the link with a button
pressed on the issuing platform. A link SHALL be recorded by pointing the
redeeming identity's `person_platform_id` row at the existing person; no
separate link table SHALL exist. Codes SHALL be six digits, single use,
expire after ten minutes and be stored hashed; a code SHALL be invalidated
after five failed redemptions across all identities; redemption failures
SHALL additionally be capped per redeeming identity (five per hour) and
globally (a per-hour ceiling that, when reached, refuses all redemptions and
alerts the operator). Either side SHALL be able to remove the link, which
re-points the removed identity at a new, empty person and carries nothing
across.

#### Scenario: Linking Discord and WhatsApp
- WHEN a Discord person gets code `482913` in their DM, writes "link 482913"
  on WhatsApp within ten minutes, and presses Confirm on the Discord prompt
  "WhatsApp account ending …3f link request, Confirm?"
- THEN the WhatsApp identity SHALL resolve to the Discord person
- AND both platforms SHALL confirm the link to that person

#### Scenario: Linking Discord and Slack
- WHEN a Slack person redeems a code issued in their Discord DM and confirms
  it on Discord
- THEN both identities SHALL resolve to one person

#### Scenario: No confirmation on the issuing platform
- WHEN a correct code is redeemed but the issuing-platform prompt is not
  confirmed within its window
- THEN no link SHALL be made

#### Scenario: A code requested in a channel
- WHEN a person asks for a link code in a public channel
- THEN no code SHALL be shown there
- AND the reply SHALL point them to a DM

#### Scenario: Guessing one code from several identities
- WHEN five wrong redemptions are made against live codes from five different
  identities
- THEN the targeted code SHALL be invalidated and its owner told to request a
  new one

#### Scenario: A global guessing wave
- WHEN failed redemptions across the deployment reach the hourly ceiling
- THEN every redemption SHALL be refused until the hour passes
- AND the operator SHALL be alerted

#### Scenario: Unlinking
- WHEN the person writes "unlink" on either platform
- THEN the removed identity SHALL lose access to the linked person's facts,
  wallets and corpus from its next question

### Requirement: A linked person's viewer is the union of each identity's live viewer

For a person with identities on several platforms, the readable channel set
SHALL be the union of each linked identity's current readable set, each
computed by that identity's own platform resolver at question time. Answers
SHALL be delivered only where the person's audience rules allow; a linked
identity SHALL NOT widen the audience of any channel reply.

#### Scenario: Access removed on one platform
- WHEN a person linked across Discord and Slack loses access to a Slack
  channel
- THEN their next question on any platform SHALL NOT use evidence from it

#### Scenario: A channel reply
- WHEN a linked person asks in a Discord channel
- THEN the visible answer SHALL still use only what that channel's audience
  can read

### Requirement: Rate limits and opt-outs apply to the person, not the identity

Per-person rate limits, corpus opt-out and conversation-memory opt-out SHALL
be keyed on the internal person id, so linking identities neither multiplies
a person's allowance nor leaves an identity outside an opt-out.

#### Scenario: A linked person asking from two platforms
- WHEN a person linked on Discord and WhatsApp asks from both within one
  rate-limit window
- THEN both questions SHALL count against one allowance

#### Scenario: Opting out from one platform
- WHEN a linked person opts out of the corpus on Slack
- THEN their Discord messages SHALL also be excluded and purged
