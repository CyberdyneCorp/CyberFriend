## Context

Greenfield Python service. Two platform facts drive the whole shape of this design:

- **Discord exposes no stable message-search API to bots.** The search endpoint is preview-only and absent from `discord.py`. There is no way to answer a retrospective question by asking Discord at query time, so we must hold the corpus ourselves.
- **Slack, which this repository will add later, is the opposite.** Its `assistant.search.context` (Real-time Search API, Feb 2026) searches workspace content on a bot token and enforces the caller's permissions itself, while `conversations.history` for new non-Marketplace apps is throttled to 1 request/minute at 15 messages/page — making bulk backfill through it impractical.

So retrieval is *structurally different* per platform: own the index for Discord, delegate to the platform for Slack. Committing to a single monolithic implementation now would have to be undone later. That asymmetry is the main thing this design accommodates.

Chosen constraints: team-wide and permission-aware; an OpenAI-compatible LLM endpoint so the provider stays swappable; Python.

## Goals / Non-Goals

**Goals:**

- A faithful, current Discord corpus — including edits and deletions — with resumable backfill.
- Permission enforcement that is structurally impossible to bypass, not merely remembered.
- Retrieval that works for both exact-term and paraphrased queries, and that treats "this week" as a filter.
- One retrieval implementation, consumed over MCP, that later surfaces reuse unchanged.

**Non-Goals:**

- Structured extraction of asks/commitments and scheduled digests (next change).
- A Discord bot surface and natural-language query router (next change).
- Slack (later change) — but the ports here must not assume Discord.
- Voice, image understanding, or acting on the user's behalf.

## Decisions

### Ports and adapters, with the search port as the hinge

The core depends on protocols — `ChatSource`, `SearchBackend`, `AclResolver`, `EmbeddingClient`, `Store` — and knows nothing about Discord. `SearchBackend` is the load-bearing one: `PgVectorSearch` (we own the data) and, later, `SlackNativeSearch` (delegates to `assistant.search.context`) are genuinely dissimilar implementations of one contract. Getting this boundary right now is what makes Slack an adapter rather than a rewrite.

This also matches the Hexagonal Architecture the repo's review process expects of backend code.

### PostgreSQL with pgvector as the single store

Messages, identities, windows, embeddings, and cursors live in one database. Postgres provides lexical search (`tsvector`) and vector search (`pgvector`) together, so hybrid retrieval needs no second system to keep consistent, and — critically — the permission filter is a `WHERE channel_id = ANY(...)` predicate in the *same* query as the ranking. A separate vector store would force filtering to happen after retrieval, which is exactly the weakness we are designing against.

*Alternative considered:* a dedicated vector database. Rejected — it splits the source of truth, and post-hoc permission filtering silently degrades result counts.

### Embed windows, not individual messages

Chat messages are short fragments ("yeah", "can you look at it?") whose embeddings carry almost no signal. The retrieval unit is a *window*: a thread, or a rolling span of messages split on a silence gap. This single choice separates a system that returns useful results from one that returns noise, and it is the main reason the existing open-source Discord RAG bots disappoint.

Windows are derived data — they are rebuilt when their constituent messages change, and messages remain the source of truth.

### Permissions resolved at query time, enforced in SQL

Discord permissions are computed, never stored: `@everyone` base, OR'd role grants, then per-channel overwrites applied in a documented order. We do not reimplement this — `discord.py` already implements it correctly as `channel.permissions_for(member)` over its warm guild cache. We resolve the viewer's visible channel set at query time (checking both `view_channel` and `read_message_history`) and pass it into the query as a filter.

Resolving at query time rather than denormalising into stored ACL rows means a revoked role takes effect on the next query with no reindexing, and there is no stale-permission window.

The enforcement mechanism matters more than the rule: **the retrieval port takes `viewer` as a required argument, so an unfiltered query is unrepresentable in the type system.** Permission enforcement expressed as a prompt instruction, or applied to results after ranking, is not acceptable — both fail open.

### Two LLM roles behind one OpenAI-compatible client

A single client configured by `base_url`, with distinct model names for embedding and for future reasoning work. This keeps AminiLLM (self-hosted Qwen3), OpenAI, and a LiteLLM proxy in front of Claude interchangeable without code changes — which matters because message history is internal company conversation and the deployment may need to stay on-premises.

Embedding dimensionality is recorded in configuration and in the schema; changing the embedding model is a reindex, not a hot swap.

### MCP as the only consumer-facing surface

Retrieval ships as MCP tools rather than an HTTP API. The immediate payoff is that this change is useful the moment it lands — pointed at a Claude Code session, it answers questions with no bot written yet. A smart client does its own query routing, so the router only becomes necessary for the Discord bot surface, which is why it belongs in the next change rather than this one.

### Ingestion: live plus reconciling backfill

Live capture via the gateway, historical import via paginated REST reads with per-channel watermarks. Both converge on an idempotent upsert keyed by platform message ID, so a reconnect that replays messages cannot duplicate them. Deletions that occur while the process is down are caught by a periodic reconciliation pass over recent history, since the gateway event is missed entirely in that case.

### The permission predicate is not a field on the query object

The obvious design puts everything the search needs into one `SearchQuery` — terms, date range, author, channels. That design mixes two things that must never mix: the **viewer's authorization** and the **viewer's intent**. Once they share a field, any code that rewrites the query can rewrite the permission filter, and safety depends on nobody doing so.

That matters here because the next change adds a corrective loop whose whole job is rewriting queries to widen a search. A "broaden the filters" step is entirely reasonable against intent and catastrophic against authorization.

So:

- `SearchQuery` carries **intent only** — terms, time range, author, channel *preference*. It is freely rewritable.
- The viewer's readable-channel predicate is **not a field on that type at all**. The store adapter applies it from the viewer context.

A rewriting loop then cannot widen the permission filter — not because a policy forbids it, but because the predicate is not a member of the type the rewriter operates on. Impossibility by typing rather than by rule.

The viewer must additionally be **required with no default** on the context that carries it. A context that can be constructed without a viewer must fail to construct. Defaulting to an empty set fails safe but hides the bug; defaulting to "all channels" is the exfiltration bug itself.

*Credit: Athena, who found a live counterexample in their own codebase — a corrective action that drops all filters wholesale, reachable at every classification level, prevented from firing only by the fact that no shipped configuration enables it. Convention, not invariant.*

### Permission filtering happens inside the search, not after it

Filtering an approximate nearest-neighbour scan *after* it returns is the sharpest trap in this design, and it fails silently: ask the index for 40 candidates, it returns its 40 best, the permission filter drops 35, and the viewer gets an answer built from 5 — with no error anywhere. The people most affected are exactly those in the fewest channels, who simply receive quietly worse answers.

So the visible channel set is passed *into* the query as a predicate, with the filter attributes denormalised onto the window rows so the restriction constrains what the index examines. Two concrete consequences:

- `hnsw.iterative_scan` must be set explicitly (`relaxed_order`). It defaults to `off`, and with a selective filter and iterative scan off, under-return is guaranteed by construction rather than occasional.
- The corresponding test asserts *result count*, not just result membership — a membership-only test passes happily while the system under-returns.

*Credit: this came from the Athena team's production experience; it would otherwise have shipped.*

### Score provenance is recorded alongside every score

Lexical, vector, fused, and (later) reranked scores are not the same number and are not comparable. Reciprocal Rank Fusion in particular produces values like `0.016` for a **first-place** result — a number that reads like "1.6% relevant" to anyone who encounters it without context, including a future reasoning loop deciding whether evidence is sufficient.

Every score therefore carries the method that produced it, and no threshold is ever applied to a score without checking its provenance.

### Chunk sizing in tokens, not characters

Window size is bounded in tokens using the embedding model's own tokenizer. Character-based sizing makes each window's real size depend on the corpus's characters-per-token ratio, which for Discord — emoji, code snippets, multiple languages, accented text — varies wildly. The practical result of getting this wrong is silent truncation against the embedding model's budget.

### One identity domain for deduplication

"Have I already stored this message?" is answered by exactly one comparison: platform message ID plus edit timestamp. Deduplication must never compare a hash computed over parsed content against a signature computed over raw content — those live in different domains, can never match, and produce a system that reprocesses its entire corpus on every sync while appearing to work.

## Risks / Trade-offs

- **This is a permanent searchable archive of everything the team says.** The governance work — retention window, disclosure, opt-out — is a precondition for running it anywhere real, not a follow-up. Deletion propagation is specified as a hard requirement for the same reason: a deleted message resurfacing is the failure that ends trust in the tool.
- **A permission defect leaks private-channel content.** Mitigated by making unfiltered retrieval unrepresentable and by testing the invariant at the repository layer rather than by inspecting generated answers. This is the highest-value test in the suite.
- **Query-time permission resolution costs a cache lookup per query** and depends on `discord.py`'s guild cache being warm. Accepted: correctness on revocation is worth more than the latency, and a cold cache is a startup concern, not a steady-state one.
- **Windowing parameters (span, gap) are guesses** until measured against real conversation. They are configuration, and the retrieval test set exists to tune them; expect to revise them once there is a real corpus.
- **Backfilling a large, old server is slow** and the first useful results may lag installation by hours. Mitigated by backfilling newest-first, so recent history — which is what people actually ask about — becomes queryable early.
- **Bot accounts cannot read direct messages between people, on any platform, by design.** Questions of the form "what did people ask me?" therefore cover indexed channels and DMs sent to the bot, and nothing else. There is no compliant workaround; this needs to be stated plainly to users rather than discovered by them.
