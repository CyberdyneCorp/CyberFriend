## ADDED Requirements

### Requirement: The deployment mode decides what Slack data may be stored

The Slack adapter SHALL run in one of two modes set by `SLACK_MODE`:
`internal` (default), for one organisation's own app, which MAY keep the
message index; and `federated`, for installs by other organisations, which
SHALL NOT persist Slack messages, windows, embeddings or files beyond an
answer-time cache deleted when the answer is sent. No mode SHALL use Slack
data to train or fine-tune a model.

#### Scenario: An install from another organisation in internal mode
- WHEN an install or event arrives for a team or enterprise other than the
  configured `SLACK_TEAM_ID` or `SLACK_ENTERPRISE_ID` in internal mode
- THEN it SHALL be rejected, nothing from it SHALL be stored, and the attempt
  SHALL be logged for the operator

#### Scenario: Federated mode answers without storing
- WHEN a person asks a question in federated mode
- THEN retrieval SHALL use Slack's Real-time Search with that person's user
  token
- AND after the answer is sent no Slack message content SHALL remain in the
  database, in Langfuse traces, in `trace_export` rows, in conversation-memory
  turns or in log output
- AND the stored memory turn SHALL hold only the asker's own question and the
  answer text, without citations or quoted evidence

#### Scenario: Federated mode not yet built
- WHEN `SLACK_MODE=federated` is set before the federated phase ships
- THEN the process SHALL refuse to start with a message naming the missing
  phase

### Requirement: Every Slack request is authenticated and acknowledged in time

Events, slash commands and interactivity payloads SHALL be accepted only over
an authenticated transport: a Socket Mode connection opened with the app-level
token, or HTTP requests whose `X-Slack-Signature` verifies against the signing
secret with a timestamp no older than five minutes. Every payload SHALL be
acknowledged within 3 seconds, before any model or network work, and
redelivered events SHALL be processed once. Each envelope SHALL be recorded
durably, keyed by its `event_id` (or `envelope_id` for commands and
interactivity), before it is acknowledged, and a worker SHALL process every
recorded envelope that has not been marked processed, so an acknowledged
envelope is never lost. Exactly one process SHALL hold the Slack connection
for a deployment.

#### Scenario: A forged HTTP request
- WHEN a request to the Slack endpoints has a missing or wrong signature, or a
  stale timestamp
- THEN it SHALL be rejected with 401 and SHALL NOT be processed

#### Scenario: A slow answer
- WHEN a question needs more than 3 seconds to answer
- THEN the payload SHALL already have been acknowledged
- AND the answer SHALL be delivered afterwards through `response_url` or the
  Web API

#### Scenario: A retried event
- WHEN Slack redelivers an event with an `event_id` already processed
- THEN no message SHALL be stored twice and no reply sent twice

#### Scenario: A process restart after ack still answers the event
- WHEN the process stops after acknowledging a DM question and before
  answering it
- THEN after restart the question SHALL be answered once

#### Scenario: A retried event after a restart
- WHEN Slack redelivers an already-processed `event_id` after a restart
- THEN it SHALL still be recognised as processed and nothing SHALL be sent
  twice

#### Scenario: Two processes never split the event stream
- WHEN the bot and ingest processes are both running
- THEN only the Slack runtime process SHALL open a Socket Mode connection
- AND every event SHALL reach the ingest and controller paths through it

#### Scenario: Socket connection refresh
- WHEN Slack asks the Socket Mode client to refresh its connection
- THEN the client SHALL reconnect without losing unacknowledged envelopes it
  had already accepted

### Requirement: Live capture covers indexed channels only and honours edits and deletes

In internal mode, messages posted in indexed channels the bot is a member of
SHALL be stored with their thread, edits SHALL replace content, and deletes
SHALL tombstone the message, rebuild its windows and withdraw asks that
depended on it. Messages in channels outside the scope, bot messages and the
assistant's own messages SHALL NOT be indexed. Direct and group-direct
conversations with the bot SHALL NOT be indexed into the corpus.

#### Scenario: An edited message
- WHEN a `message_changed` event arrives for a stored message
- THEN its content and edit time SHALL be updated and the affected windows
  rebuilt

#### Scenario: A deleted message
- WHEN a `message_deleted` event arrives for a stored message
- THEN the message SHALL no longer be retrievable, citable or summarised
- AND asks extracted from it SHALL be withdrawn

#### Scenario: A thread reply
- WHEN a reply with a `thread_ts` is posted in an indexed channel
- THEN it SHALL be stored in that thread and windows SHALL follow the thread

#### Scenario: A channel outside the scope
- WHEN a message is posted in a channel the bot is in but that is not indexed
- THEN it SHALL NOT be stored

### Requirement: Backfill respects Slack's limits and resumes exactly

When a channel is added to the scope in internal mode, history SHALL be
backfilled newest-first up to `SLACK_BACKFILL_DAYS` (default 90) through
`conversations.history` and `conversations.replies`, staying under the
method's rate tier, honouring `Retry-After`, and resuming from the stored
opaque cursor after an interruption.

#### Scenario: Rate limited
- WHEN Slack answers a backfill request with 429 and `Retry-After: 30`
- THEN the next request for that method SHALL wait at least 30 seconds
- AND the backfill SHALL continue without skipping a page

#### Scenario: Interrupted backfill
- WHEN the process restarts in the middle of a backfill
- THEN it SHALL resume from the stored cursor with no gap and no duplicate

#### Scenario: The bot was removed from the channel
- WHEN a backfill request fails with `not_in_channel` or `channel_not_found`
- THEN the backfill SHALL stop, the channel SHALL be marked unavailable in the
  ingest status, and nothing SHALL be retried in a loop

#### Scenario: Recent history first
- WHEN a backfill is in progress
- THEN messages from the most recent day SHALL be retrievable before older
  pages finish

### Requirement: Deletion by retention policy is reconciled

Because Slack retention purges do not reliably emit delete events, the
adapter SHALL periodically reconcile the recent window of each indexed
channel, fetching both `conversations.history` and `conversations.replies`
for each thread in the window. It SHALL tombstone a stored message only when
the whole window was fetched without error and Slack no longer returns it,
and SHALL tombstone nothing for a channel whose fetch failed in any way.
Messages older than the deployment retention policy (`app/retention.py`)
SHALL be purged by the existing retention job, as on Discord.

#### Scenario: A purged message
- WHEN a complete reconcile of the window does not return a stored message
  within it
- THEN the message SHALL be tombstoned as if a delete event had arrived

#### Scenario: A thread reply within the window is not tombstoned
- WHEN a stored thread reply from the window is returned by
  `conversations.replies` but not by `conversations.history`
- THEN it SHALL NOT be tombstoned

#### Scenario: A reconcile that fails mid-window tombstones nothing
- WHEN a reconcile page fails, is rate limited past its retries, returns
  `missing_scope` or `not_in_channel`, or comes back short
- THEN nothing in that channel SHALL be tombstoned
- AND the failure SHALL appear in the ingest status

#### Scenario: Past the retention period
- WHEN `RETENTION_DAYS=365` and a stored Slack message is older than 365 days
- THEN the existing retention job SHALL purge it

### Requirement: Shared files are ingested with the bot's own authorisation

Files shared in indexed channels SHALL be downloaded from `url_private` with
the bot token and passed to the existing document pipeline, scoped to the
channel they were shared in. `file_deleted` and `file_unshared` SHALL remove
the document from the channels it no longer belongs to.

#### Scenario: A PDF shared in an indexed channel
- WHEN a PDF is shared in an indexed channel
- THEN its content SHALL become retrievable by people who can read that
  channel, and by nobody else

#### Scenario: A deleted file
- WHEN a `file_deleted` event arrives for an ingested file
- THEN the document SHALL no longer be retrievable

### Requirement: Installs, tokens and uninstalls are managed and auditable

Slack tokens SHALL be stored per team or enterprise, encrypted at rest, never
logged, and redacted in the admin console and traces. An `app_uninstalled`
event, or a `tokens_revoked` event naming the bot token, SHALL delete the
install's tokens at once, stop ingestion for that install, and have its
stored Slack data purged by the retention job's next run. A `tokens_revoked`
event naming only user tokens SHALL delete those user tokens and nothing
else. Operators SHALL see each install and each indexed channel's ingest
status in the admin console.

#### Scenario: App uninstalled
- WHEN an `app_uninstalled` event arrives, or `tokens_revoked` names the bot
  token
- THEN the install's tokens SHALL be deleted immediately
- AND its messages, windows, documents, membership cache and inbound rows
  SHALL be purged by the next retention run

#### Scenario: User token revoked
- WHEN `tokens_revoked` names only one person's user token
- THEN that token SHALL be deleted
- AND nothing else SHALL be purged

#### Scenario: Channel deleted
- WHEN a `channel_deleted` event arrives for an indexed channel
- THEN its stored messages SHALL be tombstoned and its documents and asks
  withdrawn, and it SHALL leave the scope

#### Scenario: Channel archived
- WHEN an indexed channel is archived
- THEN its stored content SHALL stay retrievable under the same membership
  rules

#### Scenario: Admin console view
- WHEN an operator opens the Slack page of the admin console
- THEN they SHALL see each install's team, enterprise and scopes, and each
  indexed channel's last event, backfill progress and last error
- AND no token SHALL be shown

### Requirement: Group direct messages are answered only when addressed and never stored

In an `mpim` that includes the assistant, the assistant SHALL answer only a
message that mentions it, and SHALL NOT store, window, index or keep in
conversation memory the other members' messages; only the asker's own
addressed turn and the reply MAY be kept.

#### Scenario: An mpim message without a mention
- WHEN a member of an mpim with the assistant writes without mentioning it
- THEN no reply SHALL be posted and nothing from the message SHALL be stored

#### Scenario: An mpim message with a mention
- WHEN a member mentions the assistant in the mpim
- THEN it SHALL answer under group-direct audience rules
- AND the other members' earlier messages SHALL NOT appear in conversation
  memory
