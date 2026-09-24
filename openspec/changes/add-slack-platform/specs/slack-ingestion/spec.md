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
  database

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
redelivered events SHALL be processed once.

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

For workspaces with a retention policy, the adapter SHALL periodically
reconcile recent history for each indexed channel and SHALL tombstone
messages that Slack no longer returns or that are older than the configured
`SLACK_RETENTION_DAYS`, because retention purges do not reliably emit delete
events.

#### Scenario: A purged message
- WHEN the reconcile finds a stored message within its window that Slack no
  longer returns
- THEN the message SHALL be tombstoned as if a delete event had arrived

#### Scenario: Past the retention period
- WHEN `SLACK_RETENTION_DAYS=365` and a stored message is older than 365 days
- THEN it SHALL be tombstoned

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
logged, and redacted in the admin console and traces. An uninstall or token
revocation SHALL stop ingestion for that install and SHALL delete its stored
Slack data within the retention job's next run. Operators SHALL see each
install and each indexed channel's ingest status in the admin console.

#### Scenario: App uninstalled
- WHEN an `app_uninstalled` or `tokens_revoked` event arrives
- THEN the install's tokens SHALL be deleted immediately
- AND its messages, windows, documents and membership cache SHALL be purged

#### Scenario: Admin console view
- WHEN an operator opens the Slack page of the admin console
- THEN they SHALL see each install's team, enterprise and scopes, and each
  indexed channel's last event, backfill progress and last error
- AND no token SHALL be shown
