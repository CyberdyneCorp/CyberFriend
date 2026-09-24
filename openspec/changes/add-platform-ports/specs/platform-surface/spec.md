## ADDED Requirements

### Requirement: Each platform declares its capabilities and the core branches only on them

Every registered platform SHALL declare a `PlatformCapabilities` value
(channels, threads, history backfill, ephemeral replies, slash menu, commands
in threads, button limit, list picker, edits and deletes as events,
reactions, plain and rich message length, markup dialect, proactive-message
policy, permalinks). Application services SHALL decide behaviour from these
capabilities and the conversation kind, and SHALL NOT compare platform names.

#### Scenario: A service needs to know whether channels exist
- WHEN a feature depends on channels
- THEN it SHALL read `has_channels` from the asker's platform capabilities

#### Scenario: Declared capabilities are pinned
- WHEN the test suite runs
- THEN a golden test per registered platform SHALL assert its declared
  capabilities, so a change to any flag is a reviewed diff

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
- WHEN model output contains a mass mention in any platform's syntax,
  including a user-group mention such as Slack `<!subteam^S0123>`
- THEN the rendered message SHALL NOT notify the channel or the group on any
  platform

#### Scenario: A person mention in model output
- WHEN model output contains a person mention that the controller did not
  place there
- THEN it SHALL be rendered as the person's plain name and nobody SHALL be
  notified

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

Conversations with more than one human participant (Slack mpims, and later
WhatsApp business-created groups) SHALL be delivered as `GROUP_DIRECT`, whose readable set is
the intersection over its members and which SHALL NOT count as private for
DM-only facts.

#### Scenario: Asking for one's phone in a group DM
- WHEN a person asks for their saved phone number in a Slack mpim
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

### Requirement: Progress indication lasts as long as the work

The progress indicator SHALL stay visible until the reply is sent. A platform
whose indicator expires on its own SHALL declare a refresh interval, and the
controller SHALL re-send the indicator at that interval until the reply is
sent or the run ends.

#### Scenario: A long run on WhatsApp
- WHEN an agent run on WhatsApp takes 70 seconds
- THEN the typing indicator SHALL have been re-sent about every 20 seconds
  until the reply

### Requirement: Proactive messages go where the item was created unless the person chooses otherwise

Alert firings, scheduled answers and notifications SHALL be delivered on the
platform the item was created on, unless the person has set a preferred
delivery platform among their linked identities, in which case they SHALL be
delivered there. If delivery on the chosen platform returns `CLOSED`, the
item SHALL be treated as closed there and SHALL NOT silently move to another
platform.

#### Scenario: An alert created on Discord after linking WhatsApp
- WHEN a person created an alert on Discord, later linked WhatsApp, and has
  set no preferred platform
- THEN the firing SHALL be delivered on Discord

#### Scenario: A preferred delivery platform
- WHEN the same person sets WhatsApp as their preferred delivery platform
- THEN the next firing SHALL be delivered on WhatsApp, under WhatsApp's window
  and opt-in rules

### Requirement: Secret key material is never stored

When the secret-material guard is enabled for a platform (`SECRET_GUARD_PLATFORMS`,
default `whatsapp`, so Discord output is unchanged until enabled there), a
message that contains a BIP-39 mnemonic (12, 15, 18, 21 or 24 words from the
word list whose checksum verifies) or a private key (a 64-hex-digit string,
with or without `0x`, framed as a key by words such as "private key", "chave
privada" or "pk:") SHALL NOT be stored in conversation memory, traces, trace
export, inbound dedup rows or logs, and SHALL get a warning reply. A
64-hex-digit string that is not framed as a key, or that is a known
transaction hash, SHALL be treated as ordinary text.

#### Scenario: A seed phrase
- WHEN a person sends twelve BIP-39 words whose checksum verifies
- THEN the message SHALL NOT be stored in conversation memory or traces
- AND the reply SHALL warn them not to share it

#### Scenario: A pasted transaction hash is not refused
- WHEN a person pastes a bare `0x`-prefixed 64-hex transaction hash and asks
  what it did
- THEN the wallet-activity answer SHALL be given as usual

#### Scenario: A framed private key
- WHEN a person writes "my private key is 0x…" with 64 hex digits
- THEN the message SHALL NOT be stored and the reply SHALL warn them

### Requirement: Speech is transcribed through one port

Audio from any platform SHALL be transcribed through a single `SpeechToText`
port. The default implementation SHALL be a null transcriber that reports
"unavailable", so a platform without a configured backend answers a voice
message by asking for text. Audio SHALL be deleted once transcribed, and the
chosen backend SHALL be recorded as a data-flow decision because audio is
personal data.

#### Scenario: No backend configured
- WHEN a voice message arrives and only the null transcriber is registered
- THEN the reply SHALL ask the person to write instead
- AND the audio SHALL NOT be stored
