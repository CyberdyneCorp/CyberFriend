## Context

The deployed Langfuse is `langfuse/langfuse:3` (currently 3.225.8), with
ClickHouse and MinIO. It runs as a separate Coolify service stack,
`cyberfriend-langfuse`, not in this repo's `docker-compose.yml`. Its v1 public
read APIs are available: `/api/public/metrics`, `/api/public/traces` and
`/api/public/observations`. The v2 metrics and observations endpoints need the
v4 data model and are not usable here.

`LangfuseTracer._batch` sends one `trace-create` per run from
`ReasoningAnswerService._recorded`. That trace carries `userId` = Discord
platform user id, `input.question`, `output.answer`, and metadata including
counts, decisions and the full evidence (up to `MAX_EVIDENCE_CHARS` of text per
item, `langfuse.py` `_evidence`). The tracer already sends an `environment`
(default `production`), but nothing reads by it.

## Goals / Non-Goals

**Goals:**

- Cost and usage per person and per feature, with tokens and estimated cost
  that Langfuse computes from real per-call usage.
- Question text: admin-only, OIDC-only, audited, never stored outside
  Langfuse, disclosed actively, and expiring.
- A person who opted out, erased their data or is being erased never appears,
  in counts or in text, even while Langfuse's own deletion is still queued.
- The simplest read path that works for a console used by a few admins.

**Non-Goals:**

- Exact billing. Totals are estimates from model price tables.
- Background job spend (a follow-up; the naming is reserved).
- Showing counts while Langfuse is down.

## Decisions

### Feature id decided on the domain side

`RunRecord` gains `feature: str`, set where the route is decided (the router
and the answer service), not inferred from strings in the adapter. The trace
`name` is the feature, and the path stays in metadata. The initial set:
`corpus.fixed`, `corpus.loop`, `market.price`, `market.other`,
`wallet.balance`, `wallet.activity`, `portfolio`, `defi.positions`,
`web.search`, `time`,
`obligations`, `decisions`, `capabilities`, `federation`. New features add a
constant, and a test asserts that every route decision maps to one.

### Tags and environment: scoping every read and delete

- Every trace carries tag `app:cyberfriend` and the environment from
  `LANGFUSE_ENVIRONMENT` (default `production`).
- `feature:<id>`, `path:<fixed|loop>`, `lang:<xx>`, and `tool:<qualified_name>`
  for each federated tool called. The v1 metrics API groups by `tags`, and the
  traces list filters by them.
- Every read (metrics, traces) and every delete query this change adds filters
  on `environment = LANGFUSE_ENVIRONMENT` and tag `app:cyberfriend`. Nothing
  here ever reads or deletes another app's or environment's traces in a shared
  Langfuse project.

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

### Evidence as references only

Evidence text quotes other people's messages, including private channels and
DMs, and anyone with a Langfuse UI login could read it. The trace now carries,
per evidence item, only the window id, channel id, source system and score.
`trace_export_message` still indexes which messages a trace drew on, so
deleting a message still withdraws the traces built from it (their answers can
paraphrase it). Langfuse UI access is limited to the same admin set that holds
the console admin role, as an ops rule in `docs/operations.md`.

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
It is a documented ops step, idempotent, and not run at startup.

### Live reads through a port, with a short cache

The user's decision is that the admin API queries Langfuse server-side. There
is no rollup table and no sync loop.

- **Port:** `ports/usage.py` has `UsageSource.aggregate(window, dimension)` and
  `UsageSource.questions(user_id, window, page)`. A rollup could be added
  behind it later if latency is a real problem.
- **Adapter:** `adapters/tracing/langfuse_usage.py`, built with the admin
  process's injected transport.
  - Aggregates use `GET /api/public/metrics`. Traces view: dimensions
    `[userId, name]`, metrics `[count]`. Observations view (generations):
    dimensions `[userId, providedModelName]`, metrics `[inputTokens,
    outputTokens, totalCost]`. Observations view (spans): dimensions
    `[userId, name]`, metrics `[count]`. Every query groups by `userId`, so
    exclusion can be applied before anything is summed.
  - `row_limit` is 1000. A window whose result reaches it is split in halves
    until it fits.
- **Cache:** an in-process TTL cache (5 minutes) keyed by window and
  dimension, holding the per-user rows before exclusion. Exclusion runs on
  every request against the current database, so an opt-out or erasure takes
  effect at once, not after the cache expires. Question text is never cached.
- **Unavailable:** when Langfuse cannot be reached the summary returns 503
  `{"error": "usage unavailable"}` and the screen says so.
- **Display names** are joined at read time from `person_platform_id` and
  `person`. An unmatched id is shown raw.
- **Voice minutes** come from `media_usage` (per person, filtered like the
  rest) plus the anonymous erased total (server total only), shown as their
  own feature.

### Exclusion, applied on every request

`UsageExclusions.load()` reads, from our Postgres, on every request:

- platform ids of people in `person_opt_out`: excluded entirely;
- platform ids of people with an open `erasure_request`: excluded entirely;
- platform ids of people with a completed erasure: rows up to and including
  the day of `person.erased_before` are excluded (day granularity for counts;
  exact timestamp for text);
- for the questions endpoint, trace ids in `trace_export` with
  `deletion_requested_at` set.

Aggregates drop excluded user rows before summing. The questions endpoint
returns an empty page for an excluded person, and otherwise drops excluded
trace ids. A test puts traces for an erased person and a pending-deletion
trace into FakeLangfuse and asserts neither appears in counts or text.

### Read path and gating

- `GET /api/usage/summary?from&to&group=person|feature|model|tool`: operator.
  Window capped at 90 days. Counts only.
- `GET /api/usage/people/{platform_user_id}/questions?from&to&page`:
  `admin_oidc` in the route table, meaning admin role and `via == "oidc"`. A
  `cfa_` token gets 403 whatever its role.
  - Calls `GET /api/public/traces?userId=&environment=&tags=app:cyberfriend&fields=core,io&limit=50`
    and returns only `{timestamp, feature, tools, question, input_tokens,
    output_tokens, cost}`. Answer, evidence and metadata are dropped
    server-side.
  - Only traces with `timestamp >= person.tracing_notice_at` are returned as
    text. A person who never received the notice has no readable text. Older
    traces are counted in the response (`hidden_before_notice: n`) but never
    shown.
  - Every call writes a `config_audit` entry of kind `applied` with setting
    `usage.questions_viewed`: actor `oidc:<sub>`, the viewer's email, the
    viewed person looked up server-side (person id and display name) and the
    window.
  - `Cache-Control: no-store`.
- Langfuse keys live only in server processes and are never sent to the
  browser. The key pair is project-wide (ingest, read, delete); the bot and
  ingest already hold it. `docs/operations.md` lists the admin process as a new
  holder with that rating. A separate read-only project is not possible with
  Langfuse's project-wide keys, so the mitigation is server-side only use.

### Retention

- Traces are kept 90 days (`TRACE_RETENTION_DAYS`, default 90).
- OSS self-hosted Langfuse has no retention, so the ingest process runs a daily
  sweep:
  - marks `trace_export` rows older than the cutoff as deletion-requested (the
    existing `TraceWithdrawal.retry_pending` then deletes them);
  - backstops with `GET /api/public/traces?toTimestamp=cutoff&environment=<ours>&tags=app:cyberfriend`
    and deletes the ids found, which catches traces whose index row was never
    written. Traces exported before the `app:cyberfriend` tag existed are
    matched by our trace names (`fixed`, `loop`, and the feature ids) within
    our environment.
  - Nothing outside our environment and names is ever deleted. A test puts a
    foreign-environment trace and a foreign-name trace in FakeLangfuse and
    asserts both survive.

### Disclosure: active, per person

- `person.tracing_notice_version int NULL`, `person.tracing_notice_at
  timestamptz NULL`.
- When tracing is enabled and a person who is not opted out gets a reply (DM
  or channel) and their `tracing_notice_version` is below the current version,
  the reply carries a short notice in their language (EN/PT): "Your questions
  and my answers are recorded for up to 90 days and CyberFriend admins can read
  them. Use /privacy to see or delete them." The version and time are recorded
  in the same transaction as the export, so the notice is shown once.
- A new notice version (for example a changed retention period) shows it again
  once.
- The capabilities reply states the same.
- The notice ships only after `/privacy` and "delete everything" exist, so
  every statement in it is true when it is first shown. The question-text
  endpoint ships after the notice.

### Undercount is labelled

Background LLM work and opted-out askers are not traced, so the screen labels
totals "traced question runs".

### Pinning Langfuse is an ops task

The Langfuse stack is the Coolify service `cyberfriend-langfuse`. Pinning
`langfuse/langfuse:3.225.8` and the matching worker tag is done in that
service's configuration and recorded in `docs/operations.md`. The admin
process logs a warning at startup when `/api/public/health` reports a major
version other than 3.

## Risks / Trade-offs

- [Langfuse v4 upgrade: `trace-create` ingestion returns 400 and the v1 reads
  404] -> Pin in the Coolify service. The exporter and reader sit behind
  ports. An OTLP exporter is a prerequisite for any v4 upgrade, and ops docs
  say so.
- [Live queries are slow for a 90-day window] -> 5-minute cache and a few
  admins. The port leaves room for a rollup if this proves wrong.
- [Langfuse deletion is queued, so erased people's traces still exist for a
  while] -> Exclusion is applied from our database on every request, not from
  Langfuse's state.
- [Question text quotes other people's private messages] -> Only the asker's
  own question is returned, never evidence or answers, and evidence text is no
  longer exported at all. OIDC admin only, audited, no-store.
- [Cost estimates depend on the price table] -> The price table is checked in
  and reviewed like code.
- [Traces before the tag existed] -> Matched by environment plus our trace
  names, never by time alone.
