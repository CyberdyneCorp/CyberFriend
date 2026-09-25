## Why

A person has no way to see what the bot holds about them, and no way to remove
it on their own. Opt-out exists only in the admin console, and
`docs/operations.md` 214-218 records the gap: "nothing exposes opt-out to the
people it is for". The one self-service phrase, "forget everything you know
about me", forgets facts only, and people will read it as much more.

Planning this turned up gaps in today's opt-out, found by reading the code.
Each needs a failing test first:

- Scheduled tasks survive opt-out and keep running. `scheduled_task` has no
  opt-out purge trigger, and the due sweep does not check opt-out. This breaks
  the existing requirement "Removing a person removes their tasks".
- Opt-out never deletes Langfuse traces: neither the person's own questions nor
  traces quoting their purged messages. `trace_export` has no asker column.
- `mcp_token` and `document_fetch` rows are untouched.
- `LangfuseTraceDeleter` is built without the Edges transport, so the e2e
  harness cannot see deletions.

## What Changes

- **`/privacy`** shows, per person:
  - facts (the kinds in a guild, and the values only in a DM);
  - remembered conversation turns (counts, plus the last few in a DM);
  - scheduled tasks and alerts;
  - notification setting;
  - voice minutes this month;
  - suggestions and MCP tokens;
  - archiving on or off, and the archived channels they can read now with
    their own message count in each. Channels they cannot read are not counted
    or named (the `channel_listing` rule is unchanged);
  - fixed statements: their questions and the bot's answers are recorded for up
    to 90 days and admins can read them, and the complete list of what is kept
    after deletion (see below).

  In a guild the reply is ephemeral and shows counts only, with "Send details to
  my DM".
- **Delete everything**: one confirm flow. `/privacy` -> [Delete everything...]
  shows what will be deleted and what is kept, with two buttons, then a modal
  asking for `DELETE` or `APAGAR`:
  - **Delete everything**: erasure. The person may keep using the bot; new
    messages are archived as usual, and nothing from before the erasure is
    re-imported.
  - **Delete everything and stop archiving me**: erasure plus opt-out.

  Both erase messages, message media (attachments and transcripts), windows,
  asks, decisions, mentions, documents, facts, memory, alerts, notifications,
  scheduled tasks, suggestions, MCP tokens, linked-URL fetches, voice usage
  rows (their seconds folded into an anonymous monthly server total so the
  server-wide cap stays true), Langfuse traces the person asked and traces
  quoting their messages. The reply gives counts and says traces are
  "scheduled for deletion". It is not a permanent exit unless the person picks
  the second button.
- **Kept, stated honestly** in the dashboard, the confirmation and
  `docs/operations.md`: a minimal person record (internal id, platform ids,
  erasure time and the do-not-archive flag, name cleared); `config_audit`
  entries (append-only); Postgres backups until they age out of the backup
  retention; the CyberdyneAuth account if one was linked (no deletion API
  yet); other people's messages that mention them; other people's remembered
  answers, which may paraphrase what they said and expire within
  `MEMORY_RETENTION_DAYS`; messages the bot already sent in Discord; the
  anonymous voice total.
- **Durable erasure.** An `erasure_request` row, resumed by a sweep, so a crash
  partway through is finished and not left half done.
- **One delete path.** The opt-out purge triggers are consolidated into one SQL
  function, `purge_person_derived(person_id)`, called by the `person_opt_out`
  trigger and by erasure. Every table that holds person data (scheduled tasks
  here; suggestions, account links and sessions in later changes) adds its
  delete to that function in its own migration, so no erasure step depends on
  a table that may not exist yet.
- **Traces by asker.** `trace_export.asker_platform_user_id` is written at
  export. `request_deletion_for_asker`, plus a Langfuse `userId` query backstop
  (scoped to our environment and trace names) for traces exported before the
  column existed.
- **Opt-out now withdraws traces** (admin-console opt-out too), purges scheduled
  tasks, revokes MCP tokens and deletes linked-URL fetches for the purged
  messages.
- "Forget everything you know about me" keeps its scope (facts), and its reply
  now points to `/privacy`.

Non-goals:

- The web version of the dashboard. It comes with
  `add-account-provisioning-and-user-area` and reuses the same service.
- Deleting the CyberdyneAuth account (an open external dependency).
- A "Download my data" export. Out of scope.
- Deleting or rewriting other people's rows (their messages, their remembered
  answers).

## Capabilities

### New Capabilities

- `privacy-dashboard`: what a person can see about themselves, and self-service
  erasure.

### Modified Capabilities

- `tracing`: opting out and erasure withdraw the person's existing traces.
- `scheduled-tasks`: "Removing a person removes their tasks" gains an explicit
  opt-out scenario. The requirement already demanded this, and the code did not
  honour it.

## Impact

- New `app/privacy.py` (`PrivacyService`, reusing `OptOutService`).
- Migrations:
  - `purge_person_derived(person_id)` consolidating the opt-out purge triggers
    (0013, 0014, 0015, 0020) plus `scheduled_task`;
  - `erasure_request`; `person.erased_before`;
  - `media_usage_anonymous(month, purpose, seconds)`;
  - `trace_export.asker_platform_user_id` plus an index;
  - opt-out handling of `mcp_token` and `document_fetch`.
- Backfill skips messages authored by a person before their `erased_before`.
- Voice caps: the server-wide monthly ceiling sums `media_usage` and
  `media_usage_anonymous`.
- `adapters/tracing/langfuse.py`: record the asker. `composition.py`: build the
  deleter with Edges.
- `app/optout.py` and `admin/handlers/optouts.py`: trace withdrawal.
- Discord: the `/privacy` command, views, the modal. `self_description.py` and
  the commands snapshot.
- `docs/operations.md`: the opt-out gap closed, erasure semantics, what is
  kept, backup retention.
