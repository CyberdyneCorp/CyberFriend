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
- THEN its internal id SHALL come from a range disjoint from Discord
  snowflakes

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
