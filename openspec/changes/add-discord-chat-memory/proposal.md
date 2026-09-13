## Why

People lose track of what was asked of them across Discord channels. Answering *"what did people ask me today?"*, *"what do I need to do today?"*, or *"what happened in #infra this week?"* today means scrolling manually.

Off-the-shelf assistant runtimes (Hermes, OpenClaw) do not solve this: they route a message addressed to the bot into an agent and reply. They build no durable, searchable corpus of what was said in channels, which is what every one of those questions actually needs. And Discord offers no stable message-search API to bots — the search endpoint remains preview-only — so the corpus has to be ours.

This change builds that corpus and the permission model around it. It stops short of the conversational surfaces, which depend on it and are specified separately.

## What Changes

- Ingest Discord messages into a durable store: live capture via the gateway, plus rate-limit-aware historical backfill with resumable per-channel cursors.
- Propagate edits and deletions as tombstones so retracted content stops being retrievable.
- Resolve people to a platform-independent canonical identity, so a future Slack adapter maps onto the same person.
- Group messages into retrieval windows and index them for hybrid lexical + semantic search.
- Resolve, per asker, the set of channels that asker is permitted to read, and enforce it as a storage-layer filter on every retrieval.
- Expose retrieval as MCP tools, so any client (Claude Code, a future Discord bot, a future Slack bot) consumes one implementation.

Non-goals for this change, each deferred to its own change:

- Extracting structured "asks"/commitments and scheduled digests — this is what ultimately answers *"what do I need to do today?"*
- A Discord slash-command/mention bot surface and its natural-language query router.
- Slack ingestion and `assistant.search.context` retrieval.

## Capabilities

### New Capabilities

- `message-ingestion`: capturing Discord messages, edits, and deletions into a durable store; resumable backfill; canonical identity mapping across platforms.
- `channel-acl`: determining which channels a given person may read, and guaranteeing retrieval never returns content outside that set.
- `message-retrieval`: grouping messages into windows, hybrid lexical + semantic search over them, and returning results with verifiable citations.
- `mcp-interface`: the MCP tool surface through which clients query the corpus on behalf of an identified viewer.

### Modified Capabilities

None — this is the first change in a greenfield repository.

## Impact

- **New system.** Python service, no existing code affected.
- **New datastore:** PostgreSQL with the `pgvector` extension, holding message content, canonical identities, retrieval windows and embeddings.
- **External dependencies:** Discord Gateway and REST API; an OpenAI-compatible endpoint for embeddings (configurable `base_url`, so it may be self-hosted AminiLLM, OpenAI, or a LiteLLM proxy).
- **Discord configuration:** requires the `MESSAGE_CONTENT` privileged intent. Since 2026-06-10 the review threshold is 10,000 reachable users rather than 100 servers, so for a single internal guild this is a portal toggle rather than an application.
- **Governance, not technical.** This creates a permanent searchable archive of everything the team says in indexed channels. It needs a retention window, a disclosure to the team, and an opt-out before it runs anywhere real. Deletion propagation is specified here because a deleted message resurfacing through the bot is the failure that destroys trust in it.
- **Security surface.** The permission model is the load-bearing part: a defect leaks private-channel content to people who cannot see it. This is why ACL enforcement is specified as a storage-layer contract rather than left to prompt instructions.
