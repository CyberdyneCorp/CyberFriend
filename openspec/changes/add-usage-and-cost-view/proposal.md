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
  - tags `app:cyberfriend`, feature, path, language and each tool called, and
    the configured Langfuse environment, so every read and delete can be scoped
    to this app and environment;
  - one generation per model call, with stage, model and input/output tokens,
    so Langfuse prices it;
  - one span per federated tool call, carrying the name, outcome and latency
    and never the arguments;
  - evidence as references only (window id, channel, source, score). Evidence
    text and citation excerpts are no longer exported.
  - Answers that bypass `ReasoningAnswerService._recorded` (capabilities,
    obligations, decisions) are traced too.
- **Model prices** for models Langfuse doesn't know (the self-hosted Qwen
  models, transcription) are registered through Langfuse's models API as a
  documented, idempotent step.
- **Admin API reads Langfuse live, server-side.** No rollup tables and no sync
  loop.
  - `GET /api/usage/summary` (operator) queries Langfuse's public metrics API
    for the window, grouped by user id, removes excluded people, then
    aggregates. Results are cached in-process for 5 minutes. When Langfuse is
    unreachable the view says "usage unavailable".
  - `GET /api/usage/people/{id}/questions` (admin signed in through
    CyberdyneAuth only) fetches that person's traces and trims them to
    timestamp, feature, tools, question, tokens and cost. Answers, evidence
    and metadata never leave the server. Every read is audited per reader
    `sub`, with the viewed person looked up. Responses are `no-store`.
  - Both **always** drop people who opted out, erased their data or have an
    erasure in progress, checked against our database on every request. The
    questions endpoint also drops traces whose `trace_export` row is
    deletion-requested, and traces older than the person's disclosure notice.
- **Usage screen** in the Svelte console. Operators see counts, admins see
  question text. Voice minutes come from the existing `media_usage` ledger.
- **Retention.** Traces are kept 90 days. OSS Langfuse has no retention, so we
  add our own sweep, scoped to this app's tag and environment. It never deletes
  anything else in the Langfuse project.
- **Active disclosure.** The first time a person talks to the bot after this
  ships (DM or channel reply), the reply carries a one-time notice in their
  language (EN/PT): their questions and the bot's answers are recorded for up
  to 90 days, admins can read them, and `/privacy` shows and deletes them. The
  notice is recorded per person. A person's question text is readable in the
  console only for traces exported after that notice; older traces are
  counted, never shown.
- **Hardening found on the way.**
  - Pin the Langfuse image tags in the Coolify service (`cyberfriend-langfuse`,
    a separate stack, not this repo's compose), as an ops step.
  - Label totals as "traced question runs", because background spend is not
    traced yet.

Non-goals:

- Billing or quotas per person.
- A local usage rollup. The port allows one later if live queries are too slow.
- Tracing background jobs (extraction, embeddings, catch-up). Designed for
  (`job.<kind>` with no user) but not in this change.
- Showing answers or evidence to anyone through the console.

## Capabilities

### New Capabilities

- `usage-reporting`: the usage view, its data and who may see what.

### Modified Capabilities

- `tracing`: traces carry feature, tools and model usage, and evidence as
  references only; all answer services are traced; traces have a retention
  period; people get a one-time disclosure notice.
- `admin-console`: "The corpus is not reachable through the console" gains one
  bounded exception, a person's own question text for OIDC-signed-in admins.

## Impact

- `app/reasoning/budgets.py`, `ports.py` (`Plan`, `Grounded`), and the LLM
  adapter thread completion tokens and model names (`ModelUsage`).
- `adapters/tracing/langfuse.py`: new event types, tags and environment;
  evidence as references. New `adapters/tracing/langfuse_usage.py` (metrics and
  traces reads).
- `ports/usage.py`: `UsageSource`.
- New migration: `person.tracing_notice_version`, `person.tracing_notice_at`.
- `entrypoints/ingest.py`: trace retention sweep.
- The admin process gains the Langfuse public and secret keys. They are
  project-wide (ingest, read and delete); the bot and ingest processes already
  hold the same pair. They stay server-side and are listed in
  `docs/operations.md` with that rating.
- `self_description.py` and the reply path: disclosure text and the one-time
  notice.
- Coolify `cyberfriend-langfuse` service: pinned tags (ops, not this repo).
- `docs/operations.md`: retention, model prices, Langfuse UI access, the v4
  upgrade trap.
