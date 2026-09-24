## ADDED Requirements

### Requirement: WhatsApp offers the personal features and hides the channel features

On WhatsApp the assistant SHALL offer personal facts, conversation memory,
language handling, wallet balances, DeFi positions, portfolio, wallet
activity, crypto and FX market data, alerts, scheduled questions, personal
documents, notifications and forgetting. It SHALL NOT offer channel listing,
indexing, channel catch-up or digests, or audience-aware channel answers, and
self-description, the menu and command hints SHALL NOT mention them.

#### Scenario: Asking what the assistant can do
- WHEN a person on WhatsApp writes "what can you do?"
- THEN the reply SHALL list only features available on WhatsApp
- AND it SHALL NOT mention channels, indexing or catch-up

#### Scenario: Asking for a channel catch-up
- WHEN a person on WhatsApp asks "catch me up on #infra"
- THEN the reply SHALL say channel catch-up is not available on WhatsApp
- AND no retrieval SHALL run

#### Scenario: Asking about wallets
- WHEN a person asks "quanto eu tenho no total?" with saved wallets
- THEN the portfolio answer SHALL be given, formatted for WhatsApp

### Requirement: Team corpus is reachable only through a verified linked identity

A WhatsApp person SHALL have no readable channels unless their WhatsApp
identity is linked to a Discord or Slack identity; when linked, corpus
answers SHALL use that identity's current viewer, SHALL be delivered only in
the 1:1 chat, and SHALL NOT widen any audience.

#### Scenario: An unlinked person asks about a channel
- WHEN an unlinked WhatsApp person asks what was decided in a team channel
- THEN no corpus evidence SHALL be used
- AND the reply SHALL explain how to link their account

#### Scenario: A linked person loses access
- WHEN a linked person's Discord access to a channel is removed
- THEN their next WhatsApp question SHALL NOT use evidence from it

### Requirement: The WhatsApp assistant stays within its declared purpose

On WhatsApp only the tools in `WHATSAPP_TOOLS` SHALL be available to the
reasoning loop, with open web search, Wikipedia and general MCP servers off by
default. A question outside the declared scope SHALL get a fixed localised
reply describing what the assistant does on WhatsApp, without a model answer.

#### Scenario: An open-domain question
- WHEN a person asks "write me a poem about Lisbon" on WhatsApp with the
  default tool list
- THEN the fixed scope reply SHALL be sent and no model call SHALL be made to
  answer it

#### Scenario: The operator enables web search
- WHEN the operator adds web search to `WHATSAPP_TOOLS`
- THEN web search SHALL be available on WhatsApp and nowhere else changes

### Requirement: The assistant discloses who processes WhatsApp messages

The first reply to a new WhatsApp person and the help reply SHALL state what
the assistant is for, that Meta processes and may retain messages for up to
30 days, that seed phrases and private keys must never be sent, and how to
stop proactive messages. A message that looks like a seed phrase or private
key SHALL NOT be stored in memory or traces and SHALL get a warning reply.

#### Scenario: First contact
- WHEN a person writes to the number for the first time
- THEN the reply SHALL include the disclosure in their language

#### Scenario: A seed phrase
- WHEN a person sends twelve words from the BIP-39 word list
- THEN the message SHALL NOT be stored in conversation memory or traces
- AND the reply SHALL warn them not to share it

### Requirement: Commands work by keyword, list menu and buttons

Without slash commands, a message starting with `/word` or a localised
keyword from the command catalogue SHALL run that command; `menu` or
`help`/`ajuda` SHALL return a list message of the commands available on
WhatsApp; any other message SHALL go through natural-language routing.

#### Scenario: A keyword
- WHEN a person writes "alertas"
- THEN their active alerts SHALL be listed, in Portuguese

#### Scenario: A slash-style command
- WHEN a person writes "/schedule list"
- THEN their scheduled questions SHALL be listed
- AND the reply SHALL NOT tell them to use a slash menu

#### Scenario: The menu
- WHEN a person writes "menu"
- THEN a list message SHALL offer at most ten available commands
- AND picking a row SHALL run it

### Requirement: Confirmations use reply buttons bound to the person

Alert creation, tool approvals and forgetting one of several wallets SHALL be
confirmed with WhatsApp reply buttons (or a list for more than three choices)
whose ids reference the pending prompt. A response SHALL be accepted only from
the person the prompt was sent to and only within its window.

#### Scenario: Confirming an alert
- WHEN a person taps "Confirm" on a proposed health-factor alert
- THEN the alert SHALL be created and the reply SHALL say so

#### Scenario: A stale button
- WHEN a person taps a button of a prompt that has expired or was already
  answered
- THEN nothing SHALL change and the reply SHALL say so

#### Scenario: A forged button id
- WHEN a button reply carries a prompt id that belongs to another person
- THEN it SHALL be refused and change nothing

### Requirement: Alerts, scheduled answers and ask digests respect the window and consent

Alert firings and scheduled answers for a WhatsApp person SHALL be delivered
free-form inside the window and as a utility template outside it, only when
the person has opted in to proactive messages. Obligation notifications SHALL
be delivered only to linked people, as a daily digest.

#### Scenario: An alert fires outside the window
- WHEN an LP alert fires 40 hours after the person last wrote
- THEN the `cf_alert_fired` template SHALL be sent in their language
- AND tapping "Details" SHALL send the full alert message

#### Scenario: No opt-in
- WHEN a person who has not opted in has an alert fire
- THEN no WhatsApp message SHALL be sent
- AND the alert state SHALL still advance

#### Scenario: Creating a schedule informs about consent
- WHEN a person creates a scheduled question without having opted in
- THEN the confirmation SHALL ask them to opt in to receive it

### Requirement: Voice notes are answered through transcription when available

A voice note SHALL be transcribed through the speech-to-text port provided
by the voice-transcription feature and answered as if the transcript had been
typed, quoting the note. The audio SHALL be deleted after transcription.
Without a speech-to-text backend, the reply SHALL ask for text. Images SHALL
get a fixed reply saying they are not supported.

#### Scenario: A voice note asking for balances
- WHEN a person sends a voice note saying "what's in my wallet?"
- THEN the reply SHALL be the wallet answer
- AND the audio SHALL no longer be stored afterwards

#### Scenario: No transcription backend
- WHEN a voice note arrives and no speech-to-text port is configured
- THEN the reply SHALL ask the person to write instead

### Requirement: WhatsApp groups stay unsupported until explicitly enabled

With `WHATSAPP_GROUPS` false (the default), the adapter SHALL ignore group
messages and SHALL keep every channel capability hidden. Groups SHALL be
enabled only for an Official Business Account, and only for groups the
business created.

#### Scenario: A group message with groups disabled
- WHEN a webhook carries a message with a `group_id` and groups are disabled
- THEN it SHALL NOT be answered or stored

#### Scenario: Enabling groups without an OBA
- WHEN `WHATSAPP_GROUPS=true` is set for a number that is not an Official
  Business Account
- THEN the process SHALL refuse to start with a message saying why
