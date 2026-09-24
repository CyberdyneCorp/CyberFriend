## ADDED Requirements

### Requirement: Each platform declares its capabilities and the core branches only on them

Every registered platform SHALL declare a `PlatformCapabilities` value
(channels, threads, history backfill, ephemeral replies, slash menu, commands
in threads, button limit, list picker, edits and deletes as events,
reactions, message length, markup dialect, proactive-message policy,
permalinks). Application services SHALL decide behaviour from these
capabilities and the conversation kind, and SHALL NOT compare platform names.

#### Scenario: A service needs to know whether channels exist
- WHEN a feature depends on channels
- THEN it SHALL read `has_channels` from the asker's platform capabilities

#### Scenario: The core names no platform
- WHEN the test suite scans `domain/`, `ports/` and `app/`
- THEN it SHALL find no platform name literal
- AND the build SHALL fail if one is introduced

### Requirement: Platform-specific behaviour is reached through ports routed by platform

Rendering, reply delivery, progress indication, direct messages, choice
prompts, command registration, permalinks, mention handling, attachment
download, ACL, audience, asker profile and channel-access resolution SHALL be
ports with one implementation per platform, selected by the platform of the
reference being handled. A router SHALL fail closed for a platform with no
registered implementation.

#### Scenario: Two platforms in one deployment
- WHEN Discord and Slack are both enabled and a Slack person asks a question
- THEN the Slack ACL resolver, renderer and reply sink SHALL handle it
- AND no Discord adapter SHALL be called

#### Scenario: A missing implementation
- WHEN a port has no implementation for the reference's platform
- THEN the operation SHALL fail as unavailable
- AND an ACL or audience lookup SHALL yield no readable channels

### Requirement: Replies are produced as neutral rich text and rendered per platform

The application layer SHALL produce replies as neutral rich text, and each
platform's renderer SHALL convert it to that platform's markup, escape
platform control sequences, defang mass mentions, and split it at that
platform's length limit without breaking code blocks.

#### Scenario: A long answer on Discord
- WHEN an answer exceeds 2,000 characters on Discord
- THEN it SHALL be split exactly as before this change

#### Scenario: A mass mention in model output
- WHEN model output contains a mass mention in any platform's syntax
- THEN the rendered message SHALL NOT notify the channel on any platform

#### Scenario: A table on a platform without tables
- WHEN rich text contains a table and the platform markup has none
- THEN the renderer SHALL present it as a list without losing values

### Requirement: Private delivery respects what the platform can do

When a reply must be private to the asker, the controller SHALL use an
ephemeral reply where the platform supports it and a direct message where it
does not, and SHALL NOT fall back to a public reply.

#### Scenario: No ephemeral support
- WHEN a private reply is needed on a platform without ephemeral replies
- THEN it SHALL be sent as a direct message to the asker

#### Scenario: Direct message impossible
- WHEN neither an ephemeral reply nor a direct message can be delivered
- THEN nothing SHALL be posted publicly
- AND the failure SHALL be recorded

### Requirement: A group conversation is never treated as private

Conversations with more than one human participant (group DMs, Slack mpims,
WhatsApp groups) SHALL be delivered as `GROUP_DIRECT`, whose readable set is
the intersection over its members and which SHALL NOT count as private for
DM-only facts.

#### Scenario: Asking for one's phone in a group DM
- WHEN a person asks for their saved phone number in a group DM
- THEN the reply SHALL withhold it as it would in a public channel

### Requirement: Commands come from one catalogue and are offered only where they work

Commands SHALL be defined once in a catalogue that states what each requires.
Each platform's command surface SHALL register exactly the catalogue entries
its capabilities allow, and self-description and command hints SHALL list
only the commands available on the asker's platform and conversation kind.

#### Scenario: Surface and catalogue agree
- WHEN the test suite compares each platform's registered commands with the
  catalogue filtered by that platform's capabilities
- THEN they SHALL be equal

#### Scenario: A platform without channels
- WHEN a person on a platform with `has_channels=false` asks what the
  assistant can do
- THEN the reply SHALL NOT mention channel indexing, channel listing or
  channel catch-up

#### Scenario: Discord self-description unchanged
- WHEN a Discord person asks what the assistant can do, in a channel or a DM
- THEN the reply SHALL be identical to the reply before this change

#### Scenario: A typed command without a slash menu
- WHEN a person types a command name on a platform with no slash menu
- THEN the command SHALL be executed rather than answered with "use the menu"

### Requirement: Mentions are neutralised before routing and egress

Inbound adapters SHALL replace platform mention markup with neutral sentinels
and pass the mentioned people and channels as structured fields. Routing,
catch-up and fact-privacy rules SHALL operate on the sentinels. The egress
identifier guard SHALL reject queries containing any enabled platform's
identifier patterns or a phone number.

#### Scenario: Facts about someone else, on any platform
- WHEN a person on any enabled platform asks for another mentioned person's
  saved facts
- THEN the request SHALL be refused as it is on Discord today

#### Scenario: An identifier in a web query
- WHEN a web query would contain a Slack member id, a Discord snowflake
  mention or an E.164 phone number
- THEN the query SHALL NOT leave the deployment

### Requirement: Confirmations are platform-neutral and bound to the authenticated responder

Mutation approvals and alert confirmations SHALL go through one choice-prompt
port. A response SHALL be accepted only from the person the prompt was offered
to, as identified by the platform's authenticated event, and only within the
prompt's window.

#### Scenario: Someone else presses Confirm
- WHEN a person other than the requester responds to a choice prompt
- THEN the response SHALL be refused and change nothing

#### Scenario: An expired prompt
- WHEN a response arrives after the prompt's window
- THEN it SHALL be refused and change nothing

### Requirement: Each platform's credentials are required only when it is enabled

The deployment SHALL select platforms with `ENABLED_PLATFORMS` (default
`discord`). Settings validation SHALL require the credentials of each enabled
platform and none of a disabled one, and every secret of every platform SHALL
be registered for redaction in the audit log, the admin console and traces.

#### Scenario: Default configuration
- WHEN `ENABLED_PLATFORMS` is unset
- THEN only Discord SHALL be enabled and `DISCORD_TOKEN` SHALL be required as
  before

#### Scenario: Slack only
- WHEN `ENABLED_PLATFORMS=slack`
- THEN the process SHALL start without `DISCORD_TOKEN`
- AND it SHALL refuse to start without the Slack credentials

#### Scenario: A secret in an audit record
- WHEN any platform secret appears in a setting change
- THEN the audit record SHALL show it redacted
