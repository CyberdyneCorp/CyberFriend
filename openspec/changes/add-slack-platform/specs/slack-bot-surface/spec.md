## ADDED Requirements

### Requirement: People reach the assistant by mention, direct message or one umbrella command

The assistant SHALL answer an @mention in any conversation it is in, any
message in its `im`, and a slash command named by `SLACK_COMMAND` (default
`/cyberfriend`) whose first word selects a catalogue subcommand (`ask`,
`channels`, `index`, `unindex`, `forget`, `resolve`, `notifications`,
`schedule`, `alert`, `help`). Messages in channels that do not mention the
assistant SHALL NOT be answered.

#### Scenario: A mention in a channel
- WHEN someone writes "@CyberFriend what did we decide about the launch?" in
  an indexed channel
- THEN the assistant SHALL answer in that message's thread

#### Scenario: An unknown subcommand
- WHEN someone runs `/cyberfriend frobnicate`
- THEN the reply SHALL be ephemeral, in the asker's language, and list the
  available subcommands

#### Scenario: A channel message without a mention
- WHEN a message in a channel does not mention the assistant
- THEN no reply SHALL be posted

### Requirement: Commands answer privately unless asked otherwise

Slash-command replies SHALL be ephemeral to the invoker by default, delivered
through `response_url` or `chat.postEphemeral` after the 3-second
acknowledgement. When an ephemeral reply is impossible, the reply SHALL go to
the invoker's `im`, never to the channel.

#### Scenario: `/cyberfriend ask` in a channel
- WHEN a person runs `/cyberfriend ask what did Ana ask me today?`
- THEN only that person SHALL see the answer

#### Scenario: The response URL has expired
- WHEN the answer is ready after the response URL can no longer be used
- THEN it SHALL be sent to the person's `im`

### Requirement: Thread-bound actions work without slash commands

Because Slack slash commands cannot run inside threads, every action that
depends on a thread (resolving an ask raised in the thread, asking about the
thread, forgetting the thread's conversation) SHALL be available by
@mentioning the assistant in the thread, and asking about a message SHALL be
available as a message shortcut.

#### Scenario: Resolving an ask from its thread
- WHEN the addressee writes "@CyberFriend done" in the thread of an ask
  addressed to them
- THEN the ask SHALL be closed as `/resolve` would close it

#### Scenario: A message shortcut
- WHEN a person uses the "Ask CyberFriend about this" shortcut on a message
- THEN the assistant SHALL answer privately about that message and its thread,
  within the person's ACL

### Requirement: Indexing a Slack channel requires both Slack membership and authority

`/cyberfriend index` SHALL add a channel to the scope only when the requester
is a workspace admin or owner, the channel's creator, or listed in
`SLACK_INDEX_MANAGERS`, and the bot is a member. For a public channel the bot
SHALL join it itself; for a private channel the reply SHALL ask the requester
to invite the bot first. `/cyberfriend unindex` SHALL remove the channel from
the scope and delete what was stored from it. Indexing a Slack Connect
channel SHALL be refused when `SLACK_ALLOW_CONNECT=false`.

#### Scenario: Indexing a private channel the bot is not in
- WHEN an admin runs `/cyberfriend index` in a private channel without the bot
- THEN nothing SHALL be indexed
- AND the reply SHALL say to `/invite` the assistant first

#### Scenario: A member without authority
- WHEN a person who is not an admin, owner, creator or listed manager runs
  `/cyberfriend index`
- THEN the channel SHALL NOT be indexed
- AND the reply SHALL name who can do it

#### Scenario: Unindexing
- WHEN an authorised person runs `/cyberfriend unindex`
- THEN the channel SHALL leave the scope and its stored messages, windows and
  documents SHALL be deleted

### Requirement: Confirmations use Block Kit buttons only the requester can press

Alert creation, tool-approval prompts and forgetting one of several wallets
SHALL be confirmed with Block Kit buttons; destructive choices SHALL carry a
Slack confirm dialog. An interaction SHALL be acknowledged within 3 seconds,
accepted only from the requester identified by the authenticated payload's
user and team, and only inside the prompt's window; the prompt message SHALL
then be updated to show the outcome.

#### Scenario: Confirming an alert
- WHEN the requester presses Confirm on a proposed LP alert
- THEN the alert SHALL be created and the message updated to say so

#### Scenario: Someone else presses Confirm
- WHEN another person presses Confirm on that prompt
- THEN nothing SHALL be created and they SHALL get an ephemeral refusal

#### Scenario: Forgetting one of several wallets
- WHEN a person with three saved wallets asks to forget one without naming it
- THEN they SHALL be offered a choice of the three and a confirm button
- AND nothing SHALL be forgotten until they confirm

### Requirement: Alerts, scheduled answers and notifications are delivered to the person's DM

Alert firings, scheduled-question answers and obligation notifications for a
Slack person SHALL be sent to their `im` with the assistant, opened on demand,
respecting Slack's per-channel posting rate. A person who cannot be messaged
SHALL be treated as closed, as on Discord.

#### Scenario: An alert fires
- WHEN a Slack person's health-factor alert fires
- THEN the message SHALL arrive in their `im`, in their language

#### Scenario: The person is deactivated
- WHEN a message to a person fails because their account is inactive
- THEN delivery SHALL be recorded as closed and their alerts disabled as on
  Discord

### Requirement: Answers are formatted for Slack and cite Slack permalinks

Answers SHALL be rendered as standard Markdown through `markdown_text` up to
12,000 characters, and fixed replies and blocks as mrkdwn with Slack's
entity escaping. Mass and user-group mentions (`<!here>`, `<!channel>`,
`<!everyone>`, `<!subteam^…>`) SHALL be defanged, and person mentions
(`<@U…>`) SHALL be rendered as plain names unless the controller placed
them. Longer answers SHALL be split into several messages in the same
thread. Corpus citations SHALL link to Slack permalinks.

#### Scenario: A citation
- WHEN an answer cites a Slack message
- THEN the citation SHALL be a link that opens that message in Slack

#### Scenario: Model output with `<!channel>`
- WHEN the model's answer contains `<!channel>`
- THEN the posted message SHALL NOT notify the channel

#### Scenario: Model output with a user-group mention
- WHEN the model's answer contains `<!subteam^S1>`
- THEN the posted message SHALL NOT notify any member of that group

#### Scenario: Model output with a person mention
- WHEN the model's answer contains `<@U0123>`
- THEN the posted message SHALL show that person's name and SHALL NOT notify
  them

### Requirement: Every existing feature is available on Slack with Slack's rules

In internal mode the Slack surface SHALL offer corpus answers, channel
catch-up, asks and their notifications, conversation memory, personal facts,
language handling, documents, web and MCP federation, market data, wallet
balances, DeFi positions, portfolio, wallet activity, alerts and scheduled
questions, each satisfying the same requirements it satisfies on Discord,
with Slack mentions, reactions and threads in place of Discord's.

#### Scenario: An ask closed with a reaction
- WHEN the addressee reacts with `:white_check_mark:` to a message holding an
  ask addressed to them
- THEN the ask SHALL be closed

#### Scenario: Conversation memory in a thread
- WHEN a person asks a follow-up in the same thread
- THEN the previous turn in that thread SHALL be used as context
- AND turns from other threads of the channel SHALL NOT

#### Scenario: Catch-up
- WHEN a person asks "catch me up on #infra since Monday"
- THEN the digest SHALL cover only messages they can read, with permalinks

#### Scenario: Language from the Slack profile
- WHEN a new person whose Slack locale is `pt-BR` writes an ambiguous short
  message
- THEN fixed replies SHALL be in Portuguese

### Requirement: The Slack home and assistant pane are optional summaries

When `SLACK_APP_HOME=true`, opening the assistant's App Home SHALL show that
person's open asks, active alerts and schedules, computed under their own ACL;
when the assistant pane is enabled, suggested prompts and a working status
SHALL be shown and the status cleared when the answer is sent.

#### Scenario: Opening App Home
- WHEN a person opens the App Home tab
- THEN they SHALL see only their own asks, alerts and schedules

#### Scenario: App Home disabled
- WHEN `SLACK_APP_HOME` is false
- THEN no view SHALL be published
