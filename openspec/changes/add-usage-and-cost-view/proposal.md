## Why

Nobody can say today what the bot costs, who uses it, or for what. The data is
half there: every traced answer goes to Langfuse with the asker's Discord id,
the question and a spend record. But the cost can't be read back:

- A trace's `name` is only `fixed` or `loop`, so features are indistinguishable.
- Tokens sit in opaque metadata, and only prompt tokens at that; completion
  tokens are dropped at `Plan`/`Grounded`/`BudgetLedger`.
- There are no generation observations, so Langfuse computes no cost
  (`totalCost` is 0).
- Capabilities, obligations and decisions answers are never traced.

The team also wants to see which tools people ask for, which means question
text. That text is the most sensitive thing the console would ever show.

## What Changes

- **Traces carry what reporting needs**, still one ingestion batch per run:
  - a stable feature id as the trace name (`corpus.fixed`, `market.price`,
    `wallet.balance`, `web.search`, `capabilities` and so on);
  - tags for feature, path, language and each tool called;
  - one generation per model call, with stage, model and input/output tokens,
    so Langfuse prices it;
  - one span per federated tool call, carrying the name, outcome and latency
    and never the arguments.
  - Answers that bypass `ReasoningAnswerService._recorded` (capabilities,
    obligations, decisions) are traced too.
- **Model prices** for models Langfuse doesn't know (the self-hosted Qwen
  models, transcription) are registered through Langfuse's models API as a
  documented, idempotent step.
- **Rollup.** A sync loop in the ingest process queries Langfuse's metrics API
  every 15 minutes and writes a per-day, per-person, per-feature, per-model
  rollup to Postgres (counts, tokens, cost), plus a per-tool rollup. The rollup
  holds no text.
- **Admin API.**
  - `GET /api/usage/summary` (operator) reads only the rollup.
  - `GET /api/usage/people/{id}/questions` (admin) fetches that person's traces
    from Langfuse live and trims them to timestamp, feature, tools, question,
    tokens and cost. Evidence, answers and metadata never leave the server.
    Every call is audited, and responses are `no-store`.
- **Usage screen** in the Svelte console. Operators see counts, admins see
  question text. Voice minutes come from the existing `media_usage` ledger.
- **Disclosure and retention.** The capabilities reply and `/privacy` state
  that admins can read the questions you ask, and for how long. Question traces
  are kept 90 days and the text-free rollup 13 months. OSS Langfuse has no
  retention, so we add our own sweep.
- **Hardening found on the way.**
  - Pin the Langfuse image to `3.225.8`.
  - Build `LangfuseTraceDeleter` with the Edges transport.
  - Label totals as "traced question runs", because background spend is not
    traced yet.

Non-goals:

- Billing or quotas per person.
- Tracing background jobs (extraction, embeddings, catch-up). Designed for
  (`job.<kind>` with no user) but not in this change.
- Showing answers or evidence to anyone through the console.

## Capabilities

### New Capabilities

- `usage-reporting`: the usage view, its data and who may see what.

### Modified Capabilities

- `tracing`: traces carry feature, tools and model usage; all answer services
  are traced; traces have a retention period.
- `admin-console`: "The corpus is not reachable through the console" gains one
  bounded exception, a person's own question text for admins.

## Impact

- `app/reasoning/budgets.py`, `ports.py` (`Plan`, `Grounded`), and the LLM
  adapter thread completion tokens and model names (`ModelUsage`).
- `adapters/tracing/langfuse.py`: new event types. New
  `adapters/tracing/langfuse_usage.py` (metrics and traces reads).
- `ports/usage.py`: `UsageSource`, `UsageStore`.
- New migration: `usage_rollup`, `usage_tool_rollup`, `usage_sync_state`.
- `entrypoints/ingest.py`: usage sync loop and trace retention sweep.
- The admin process gains the Langfuse public and secret keys (read and
  delete), server-side only.
- `self_description.py`: disclosure text.
- `docker-compose.yml`: pinned Langfuse tag, admin env.
- `docs/operations.md`: retention, model prices, the v4 upgrade trap.
