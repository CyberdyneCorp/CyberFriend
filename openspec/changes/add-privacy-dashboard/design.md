## Context

`OptOutService.opt_out` (app/optout.py 131-167) sets `person_opt_out` and then
purges the person's authored messages and everything derived from them. Triggers
on `person_opt_out` purge facts (0014), memory (0013), alerts (0020) and
notifications (0015). The flag is what stops backfill re-importing the
messages (0008 33-46), so the person row must survive. Deleting it cascades the
flag away, and `retention_sql.py` 197-224 would recreate a bare person on the
next backfill. `message_media` (0026: attachments and transcripts) cascades
from `message`. `media_usage` (0025) is kept on opt-out so the server-wide
monthly ceiling stays true.

## Goals / Non-Goals

**Goals:**

- A person can see everything held about them, in a place where the values are
  safe to show.
- One confirm flow removes it all, with a choice of whether to keep using the
  bot, finishes even across crashes and outages, and says honestly what is
  scheduled, what is done and what is kept.
- One delete path shared by opt-out and erasure, extended by each table's own
  migration.
- Close the opt-out gaps with regression tests.

**Non-Goals:**

- Undo. Erasure is final. The confirmation says so.
- "Download my data".
- Touching other people's rows.

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
| message_media (on their messages) | count | count by kind (attachment, transcript) |
| feature_request | count | text and status |
| mcp_token | count | label, created |
| message (authored) | archiving on/off; channels readable now with own count | same |
| trace_export (asker) | count, plus the retention statement | same |
| person_platform_id | linked platforms | linked platforms |

Archive coverage comes from `ChannelListingService`, which lists channels that
are archived and readable now. Channels the person cannot read are neither
named nor counted, as `channel_listing.py` 11-16 requires. Erasure still
deletes the person's messages there.

Discord limits a message to 2000 characters, so the DM view uses embeds with
pages.

### One delete path: `purge_person_derived(person_id)`

A migration consolidates the bodies of the existing `person_opt_out` purge
triggers (0013 memory, 0014 facts, 0015 notifications, 0020 alerts) into one
SQL function, `purge_person_derived(person_id)`, and adds `scheduled_task` and
`mcp_token`. The
`person_opt_out` insert trigger calls it; the erasure calls it directly.

Every later table holding person data adds its `DELETE` to this function in
its own migration: `feature_request` (add-feature-requests), `account_consent`,
`account_link_code`, `person_account_link`, `user_session`
(add-account-provisioning-and-user-area). No erasure step names a table that
may not exist yet, so the PR order cannot break erasure, and admin opt-out and
self-service erasure can never drift apart.

### Erasure flow (`PrivacyService.erase(person, mode)`)

`mode` is `erase` or `erase_and_opt_out`.

1. Insert `erasure_request(id, person_id, mode, requested_at, step,
   completed_at, counts jsonb)`. At most one open request per person.
2. Stop re-import first: for `erase_and_opt_out`, insert `person_opt_out`
   (which also runs `purge_person_derived`); for `erase`, set
   `person.erased_before = now()`. Backfill and live ingestion skip messages
   authored by the person with `created_at < erased_before`. Doing this first
   closes the race with a concurrent backfill.
3. Collect the ids of every message the person authored. Mark the
   `trace_export` rows quoting them as deletion-requested (the same UPDATE as
   today's per-message withdrawal), and delete the `document_fetch` rows for
   those messages.
4. Mark every `trace_export` row with `asker_platform_user_id` in the person's
   platform ids. For traces exported before that column existed, the ingest
   process pages `GET /api/public/traces?userId=<id>&environment=<ours>&fields=core`
   for each platform id (matching our trace names) and inserts the ids found
   as pending. It picks this up from the `erasure_request` row.
5. Purge the authored messages with `OptOutService`'s message purge (messages,
   `message_media` by cascade, windows, asks, reactions, mentions, decisions,
   documents), then `SELECT purge_person_derived(person_id)` (idempotent; a
   no-op for rows the opt-out trigger already removed).
6. Fold voice usage: in one transaction, add each `media_usage` row's seconds
   to `media_usage_anonymous(month, purpose, seconds)` and delete the row. The
   server-wide monthly ceiling sums both tables, so it stays true.
7. Reduce the person row to a tombstone: display name replaced by a fixed
   placeholder, preferences reset. Kept: id, platform ids, `erased_before`,
   the opt-out flag if chosen, the disclosure-notice version.
8. Mark the request complete and reply with counts.

Each step is idempotent and records its progress in `step`. A sweep in the
ingest process (every 5 minutes) resumes open requests. The existing
`TraceWithdrawal.retry_pending` performs the Langfuse deletions in batches of
at most 100. The usage view excludes the person from the moment step 1 commits.

Langfuse v3 deletion is queued (a worker purges ClickHouse later). The reply
therefore says traces are "scheduled for deletion", and a completed request
means the deletion was accepted.

Langfuse keys: the key pair is project-wide (ingest, read and delete), and the
bot process already holds it for export. Step 4 runs in ingest because that is
where the sweep and `TraceWithdrawal` live, not because the bot lacks delete
rights. `docs/operations.md` records every process that holds the pair.

### Kept on purpose, and said so

The dashboard, the confirmation and `docs/operations.md` list every item that
survives, in the person's language:

- **A minimal person record:** internal id, platform ids, erasure time and, if
  chosen, the do-not-archive flag. Without it backfill re-imports everything.
  Name and preferences are cleared.
- **`config_audit` entries** that refer to them (for example an admin
  opt-out). The log is append-only.
- **Postgres backups**, if any, still hold the data until they age out. The
  statement comes from the `BACKUP_RETENTION_DAYS` setting: set, it names that
  period; unset (the default), it says no backups are kept. The CyberFriend
  Postgres (`cyberfriend-pgvector`) has no scheduled backups configured today,
  so the setting stays unset until Coolify backups are turned on, and
  `docs/operations.md` records this.
- **The CyberdyneAuth account**, if one was linked. CyberdyneAuth has no
  deletion API yet; the reply says how to ask its team.
- **Other people's messages** that mention them. Only the mention index goes.
- **Other people's remembered answers.** The bot's answers to other people may
  paraphrase what the person said. Those rows are not touched; they expire
  within `MEMORY_RETENTION_DAYS` (default 30).
- **Messages the bot already sent** in Discord.
- **An anonymous voice total** for the month, with no person attached.

Until the erasure flow (tasks section 4) ships, the only deletion is opt-out,
which keeps the person row with its name and notification setting and keeps
`media_usage` tied to the person. The `/privacy` kept list shipped with the
dashboard (section 3) states that, not the erasure wording above: it names the
display name and notification setting in the person record and says voice
minutes stay under the person's record. Section 4 switches it to the erasure
wording when the tombstone and the `media_usage_anonymous` fold exist.

The trace count is stated as "at least N": traces exported before 0029 have no
asker and cannot be counted per person while Langfuse still holds them.

### Confirmation: one flow, two buttons

`/privacy` has a [Delete everything...] button. It opens a
`RequesterOnlyView` (120-second timeout) that states what will be deleted and
the kept list above, with two buttons:

- **Delete everything**: "You can keep using CyberFriend. New messages are
  archived as usual; nothing from before now is re-imported."
- **Delete everything and stop archiving me**: "CyberFriend will also stop
  archiving your messages, remembering facts and conversations, answering
  voice questions and tracing your questions."

Either opens a modal asking for `DELETE` or `APAGAR`, in the person's
language. Both say the deletion cannot be undone.

### Opt-out fixes

- The `purge_person_derived` migration adds `scheduled_task` and `mcp_token`. The due sweep
  also skips opted-out people, as defence in depth.
- `OptOutService` gains the trace-withdrawal steps 3-4 and the
  `document_fetch` step; `mcp_token` goes through `purge_person_derived`. Admin
  opt-out and self-service erasure share one path.
- `trace_export.asker_platform_user_id bigint NULL` plus an index, written by
  `record_export`.
- `build_trace_withdrawal` takes an optional HTTP transport for
  `LangfuseTraceDeleter` and `LangfuseTraceFinder`. The ingest entrypoint has
  no `Edges` and passes none, so production uses httpx's default transport;
  the end-to-end harness passes FakeWeb's so a withdrawal is observable.

### Statements are true when shown

`/privacy` states the 90-day trace retention, so it ships after the retention
sweep. The one-time disclosure notice (add-usage-and-cost-view) points to
`/privacy` for deletion, so it ships after "delete everything".

## Risks / Trade-offs

- [People expect "delete everything" to stop the bot for good] -> The second
  button does that, and the first button's text says the bot keeps working.
- [Other people's remembered answers paraphrase the person] -> Stated in the
  kept list with their expiry. Rewriting other people's rows is out of scope.
- [Langfuse deletion might leave raw event blobs in MinIO] -> An ops task
  verifies that v3 trace deletion removes S3 event blobs as well as ClickHouse
  rows, and documents the result. Until it is verified, the reply says
  "scheduled for deletion".
- [Crash partway through] -> A durable request with a resume sweep.
  Idempotent steps. Re-import is stopped in step 2, before anything is purged.
- [Only platform ids can be queried in Langfuse] -> Every platform id of the
  person is queried. Traces carry only the platform user id, so this covers
  every trace of a question they asked.
- [People who opted out before 0029 lost their `message` rows to the earlier
  purge] -> The traces quoting their messages can no longer be linked to them
  (`trace_export_message` joins through `message.author_person_id`), and
  orphaned `trace_export_message` rows cannot be told apart from messages
  removed by retention, channel removal or never stored (federated and web
  ids). Accepted: `docs/operations.md` states the gap and names the manual
  cleanup, marking every trace exported before the 0029 deploy as pending.
