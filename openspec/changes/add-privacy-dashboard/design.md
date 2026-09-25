## Context

`OptOutService.opt_out` (app/optout.py 131-167) sets `person_opt_out` and then
purges the person's authored messages and everything derived from them. Triggers
on `person_opt_out` purge facts (0014), memory (0013), alerts (0020) and
notifications (0015). The flag is what stops backfill re-importing the
messages (0008 33-46), so the person row must survive. Deleting it cascades the
flag away, and `retention_sql.py` 197-224 would recreate a bare person on the
next backfill.

## Goals / Non-Goals

**Goals:**

- A person can see everything held about them, in a place where the values are
  safe to show.
- One action removes it all, finishes even across crashes and outages, and
  says honestly what is scheduled versus done.
- Close the opt-out gaps with regression tests.

**Non-Goals:**

- Undo. Erasure is final. The confirmation says so.

## Decisions

### Inventory, and where it is shown

| Store | Shown in a guild (ephemeral) | Shown in a DM |
|---|---|---|
| person_fact | kinds only (DIRECT_ONLY_KINDS never with values) | kinds and values |
| conversation_turn/summary | count per location | counts, last 5 of their own questions |
| scheduled_task | count | question, interval, last outcome |
| position_alert | count | kind, chain, address suffix |
| notification_preference | on/off | on/off, queued count |
| media_usage | minutes this month | minutes this month |
| feature_request | count | text and status |
| mcp_token | count | label, created |
| message (authored) | archiving on/off; channels readable now with own count | same |
| channels no longer readable | one aggregate count, no names | same |
| trace_export (asker) | count, plus the retention statement | same |
| person_platform_id | linked platforms | linked platforms |

Archive coverage comes from `ChannelListingService`, which lists channels that
are archived and readable now. `channel_listing.py` 11-16 forbids counting
hidden channels. The person authored the messages in those channels, so a single
aggregate with no names is shown. This is a stated exception, and an open
question for the user to confirm.

Discord limits a message to 2000 characters, so the DM view uses embeds with
pages. "Download my data" sends the same inventory as a JSON attachment.

### Erasure flow (`PrivacyService.erase(person)`)

1. Insert `erasure_request(id, person_id, requested_at, step, completed_at,
   counts jsonb)`. At most one open request per person.
2. Collect the ids of every message the person authored. Mark the
   `trace_export` rows quoting them as deletion-requested (the same UPDATE as
   today's per-message withdrawal), and delete the `document_fetch` rows for
   those messages.
3. Mark every `trace_export` row with `asker_platform_user_id` in the person's
   platform ids. For traces exported before that column existed, page
   `GET /api/public/traces?userId=<id>&fields=core` for each Discord platform
   id and insert the ids found as pending. This step runs in the ingest
   process (which holds the Langfuse keys) and is picked up from the
   `erasure_request` row. The bot process never needs Langfuse delete rights.
4. `OptOutService.opt_out(person, reason="self-service /privacy")`. This sets the flag first, then purges messages, windows,
   asks, reactions, mentions, decisions and documents. The triggers purge facts,
   memory, alerts and notifications.
5. Delete scheduled tasks and suggestions, revoke MCP tokens, delete the
   account link and consent (when the user area exists), and revoke web
   sessions.
6. Mark the request complete and reply with counts.

Each step is idempotent and records its progress in `step`. A sweep in the
ingest process (every 5 minutes) resumes open requests. The existing
`TraceWithdrawal.retry_pending` performs the Langfuse deletions in batches of
at most 100.

Langfuse v3 deletion is queued (a worker purges ClickHouse later). The reply
therefore says traces are "scheduled for deletion", and a completed request
means the deletion was accepted.

### Kept on purpose

- The person row, `person_platform_id` and `person_opt_out`. Without them
  backfill re-imports everything.
- Other people's messages that mention the person (only the mention index goes).
- `config_audit` (append-only).
- `media_usage` (accounting, decision 0025).
- The usage rollup rows are deleted. Whether to fold them into an "erased"
  bucket instead is an open question.

### Confirmation

A `RequesterOnlyView` with a 120-second timeout, then a modal asking for
`DELETE` or `APAGAR`, in the person's language. The confirmation text states
plainly that the bot will stop archiving their messages, stop remembering
facts and conversations, refuse voice questions, and stop tracing, and that
this cannot be undone. It offers `/forget everywhere` as the lighter option.

### Opt-out fixes

- Migration: a trigger on `person_opt_out` insert deletes `scheduled_task`
  rows (following 0020). The due sweep also skips opted-out people, as defence
  in depth.
- `OptOutService` gains the trace-withdrawal steps 2-3 and the `mcp_token` and
  `document_fetch` steps, so admin opt-out and self-service erasure share one
  path.
- `trace_export.asker_platform_user_id bigint NULL` plus an index, written by
  `record_export`.
- `build_trace_withdrawal` passes `edges.http_transport` to
  `LangfuseTraceDeleter`.

## Risks / Trade-offs

- [People think "delete everything" leaves the bot usable] -> The confirmation
  says it opts them out, and offers `/forget everywhere`.
- [Langfuse deletion might leave raw event blobs in MinIO] -> An ops task
  verifies that v3 trace deletion removes S3 event blobs as well as ClickHouse
  rows, and documents the result. Until it is verified, the reply says
  "scheduled for deletion".
- [Crash partway through] -> A durable request with a resume sweep.
  Idempotent steps.
- [Only Discord platform ids can be queried in Langfuse] -> Every platform id
  of the person is queried. Traces carry only the platform user id, so this
  covers every exported trace.
