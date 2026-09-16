## Why

Three things a person naturally asks the assistant for, none of which work.

"Index #design" -- today indexing scope is an environment variable, read once
when each process starts. Adding a channel means editing Coolify and
redeploying, which takes the bot offline. The admin console can write the new
scope to the database, but no running process reads it back.

"What's the latest Python release?" -- a general question reaches the web only
when the corpus finds nothing at all. When channel search returns something
loosely related, the assistant answers from that instead, and an explicit "search
the web for" is treated like any other question.

"How do I configure FastAPI dependency injection?" -- Context7, an MCP server
for library documentation, works through the full governed path locally, and is
not configured in production.

## What Changes

- **Live scope.** The bot and ingest read indexing scope from runtime
  configuration and refresh it without a restart, finishing the wiring the admin
  console already depends on.
- **Index from Discord.** A command lets a person add or remove a channel from
  indexing scope, if they hold Discord's Manage Channels permission on that
  channel. The channel is told, in the channel, that it is now archived.
- **General questions reach the web.** An explicit request to search the web
  skips the corpus. A question the corpus cannot answer well, and not only one
  it cannot answer at all, falls back to external sources.
- **MCP in production.** Context7 is configured, allowlisted read-only, and
  verified against the live deployment.

Non-goals:

- Adding or configuring MCP servers from Discord. That grants the agent new
  reach and belongs in the admin console, behind operator credentials.
- Letting anyone index any channel. See below.

## Capabilities

### New Capabilities

- `chat-indexing`: adding and removing channels from indexing scope from
  Discord, who may do it, and what the channel is told.
- `external-sources`: when a question goes to the web or to an MCP server
  instead of, or after, the corpus.
- `live-scope`: running processes applying a scope change without a restart.

### Modified Capabilities

None. `runtime-configuration` already specifies that stored settings take
effect without a redeploy; this change implements that for indexing scope.

## Impact

- **Anyone who can run the command can make a channel permanently
  searchable.** Indexing is opt-in precisely because an archive is a governance
  decision. Requiring Manage Channels on the target channel ties the decision to
  whoever already administers that channel in Discord, and posting a notice in
  the channel means its members learn it is being archived from the bot rather
  than discovering it later.
- **Removing a channel purges it.** De-indexing withdraws the channel's content
  from retrieval, as retention and opt-out already do, rather than merely
  stopping new capture.
- **More questions leave the server.** Falling back to the web on a weak corpus
  result, not only an empty one, sends more queries to external providers. They
  remain bounded to words the asker typed.
