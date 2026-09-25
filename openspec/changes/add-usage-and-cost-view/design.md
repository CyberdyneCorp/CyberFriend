## Context

The deployed Langfuse is `langfuse/langfuse:3` (currently 3.225.8), with
ClickHouse and MinIO. Its v1 public read APIs are available: `/api/public/metrics`,
`/api/public/traces` and `/api/public/observations`. The v2 metrics and
observations endpoints need the v4 data model and are not usable here.

`LangfuseTracer._batch` sends one `trace-create` per run from
`ReasoningAnswerService._recorded`. That trace carries `userId` = Discord
platform user id, `input.question`, `output.answer`, and metadata including
counts, decisions and the full evidence.

## Goals / Non-Goals

**Goals:**

- Cost and usage per person and per feature, with tokens and estimated cost
  that Langfuse computes from real per-call usage.
- The console never depends on Langfuse being fast or up to show counts.
- Question text: admin-only, audited, never stored outside Langfuse, disclosed,
  and expiring.

**Non-Goals:**

- Exact billing. Totals are estimates from model price tables.
- Background job spend (a follow-up; the naming is reserved).

## Decisions

### Feature id decided on the domain side

`RunRecord` gains `feature: str`, set where the route is decided (the router
and the answer service), not inferred from strings in the adapter. The trace
`name` is the feature, and the path stays in metadata. The initial set:
`corpus.fixed`, `corpus.loop`, `market.price`, `market.other`,
`wallet.balance`, `wallet.activity`, `portfolio`, `web.search`, `time`,
`obligations`, `decisions`, `capabilities`, `federation`. New features add a
constant, and a test asserts that every route decision maps to one.

### Tags

`feature:<id>`, `path:<fixed|loop>`, `lang:<xx>`, and `tool:<qualified_name>`
for each federated tool called. The v1 metrics API groups by `tags`, and the
traces list filters by them.

### Generations and spans

- `ModelUsage(stage, model, input_tokens, output_tokens, started_at,
  ended_at)` is collected by `BudgetLedger` for every model call.
  `completion_tokens` is threaded through `Plan` and `Grounded`, which drop it
  today.
- Each `ModelUsage` becomes one `generation-create` with the trace id,
  `name = stage`, `model` and `usageDetails`. Langfuse computes
  `calculatedTotalCost` from its price table.
- Each federated tool call becomes one `span-create`: name, outcome
  (`level = ERROR` on failure) and latency. Arguments are never included,
  because they can hold wallet addresses or queries.

All of this goes in the same ingestion batch, so there is no extra round-trip
on the answer path.

### Tracing every answer service

The tracer seam moves from `ReasoningAnswerService._recorded` to the outermost
answer service wrapper built in `composition.py`. Capabilities, obligations and
decisions then produce traces with their feature ids. The opted-out rule still
applies at the seam.

### Model prices

gpt-4o and text-embedding-3-small have built-in Langfuse prices. For models
Langfuse doesn't know (AminiLLM `chat-v1` and related, `gpt-4o-mini-transcribe`),
a `scripts/langfuse_models.py` upserts definitions through
`POST /api/public/models` (match pattern, unit prices) from a checked-in table.
It is a documented ops step, idempotent, and not run at startup, so that
deploying never needs write access to Langfuse's model table.

### Rollup in Postgres, question text live

Counts are not queried from Langfuse on each page load. ClickHouse lags,
aggregating 90 days per person is slow, and the console would inherit
Langfuse's availability. The v1 read API is also deprecated upstream, so the
query code should live behind a port with one adapter.

```
usage_rollup(day date, platform_user_id bigint NULL, feature text, model text,
             runs int, input_tokens bigint, output_tokens bigint,
             cost_usd numeric(12,6),
             PRIMARY KEY (day, platform_user_id, feature, model))
usage_tool_rollup(day, platform_user_id, feature, tool, calls, PRIMARY KEY (...))
usage_sync_state(source text PRIMARY KEY, synced_through timestamptz)
```

- **Port:** `ports/usage.py` has `UsageSource.aggregate(window)`,
  `UsageSource.questions(user_id, window, page)` and `UsageStore`.
- **Adapter:** `adapters/tracing/langfuse_usage.py`, built with
  `edges.http_transport`.
- **Sync:** a loop in the ingest process next to `trace_withdrawal_loop`,
  every 15 minutes. Each pass recomputes the last 48 hours: it deletes and
  re-inserts each day, so late writes and deletions settle. Older days are
  frozen.
  - The traces-view query uses dimensions [userId, name] and metrics
    [count, totalTokens, totalCost] at day granularity.
  - The observations-view query gets tools.
  - `row_limit` is 1000. A window that reaches it is split by day.
- **Display names** are joined at read time from `person_platform_id` and
  `person`, and are never copied into the rollup. An unmatched id is shown raw.
- **Voice minutes** come from `media_usage`, which already exists, and are
  shown beside the rollup as their own feature.

### Read path and gating

- `GET /api/usage/summary?from&to&group=person|feature|model|tool`: operator.
  Reads only the rollup. The window is capped at 90 days.
- `GET /api/usage/people/{platform_user_id}/questions?from&to&page`: admin.
  - Calls `GET /api/public/traces?userId=&fields=core,io&limit=50` and returns
    only `{timestamp, feature, tools, question, input_tokens, output_tokens,
    cost}`. Answer, evidence and metadata are dropped server-side.
  - Every call writes a `config_audit` entry of kind `applied` with setting
    `usage.questions_viewed`, carrying the viewer, whose questions and the
    window.
  - `Cache-Control: no-store`.
- Langfuse keys live only in the admin and ingest processes and are never sent
  to the browser.

### Retention

- Question traces are kept 90 days (`TRACE_RETENTION_DAYS`, default 90). The
  rollup is kept 13 months.
- OSS self-hosted Langfuse has no retention, so the ingest process runs a daily
  sweep. It marks `trace_export` rows older than the cutoff as
  deletion-requested (the existing `TraceWithdrawal.retry_pending` then
  deletes them), and it backstops with `GET /traces?toTimestamp=cutoff` ->
  bulk `DELETE`, which catches traces whose index row was never written.
- The disclosure (capabilities reply, `/privacy`) states the 90 days, that
  admins can read your questions, and that `/privacy` deletes them.
- The disclosure ships **before or with** the question-text endpoint, never
  after it.

### Undercount is labelled

Background LLM work and opted-out askers are not traced, so the screen labels
totals "traced question runs" and shows the last sync time.

## Risks / Trade-offs

- [Langfuse v4 upgrade: `trace-create` ingestion returns 400 and the v1 reads
  404] -> Pin `langfuse/langfuse:3.225.8`. The exporter and reader sit behind
  ports. An OTLP exporter is a prerequisite for any v4 upgrade, and ops docs
  say so.
- [Question text quotes other people's private messages] -> Only the asker's
  own question is returned, never evidence or answers. It is admin-only,
  audited and no-store.
- [Rollup totals change after erasures] -> Expected. An erased person's rows
  are deleted (see open question on an "erased" bucket).
- [Cost estimates depend on the price table] -> The price table is checked in
  and reviewed like code.
- [The metrics API row limit] -> Split by day, and a test covers a window that
  reaches the limit.
