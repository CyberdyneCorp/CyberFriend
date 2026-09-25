## Why

A person has no way to see what the bot holds about them, and no way to leave
on their own. Opt-out exists only in the admin console, and
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
  - archiving on or off, the archived channels they can read now with their own
    message count in each, and one aggregate count for channels they can no
    longer read;
  - three fixed statements: questions are traced and admins can read them for
    90 days; other people's messages that mention you are not deleted; voice
    minutes are kept for accounting.

  In a guild the reply is ephemeral and shows counts only, with "Send details to
  my DM". An optional "Download my data" sends a JSON file by DM.
- **Delete everything**: a button, then a modal asking for `DELETE` or
  `APAGAR`. It is a self-service opt-out plus full erasure:
  - messages, windows, asks, decisions, mentions, documents, facts, memory,
    alerts, notifications, scheduled tasks, suggestions, MCP tokens, linked-URL
    fetches;
  - Langfuse traces the person asked, and traces quoting their messages.

  The reply gives counts and says traces are "scheduled for deletion". It points
  to `/forget everywhere` for people who want to keep using the bot.
- **Durable erasure.** An `erasure_request` row, resumed by a sweep, so a crash
  partway through is finished and not left half done.
- **Traces by asker.** `trace_export.asker_platform_user_id` is written at
  export. `request_deletion_for_asker`, plus a Langfuse `userId` query backstop
  for traces exported before the column existed.
- **Opt-out now withdraws traces** (admin-console opt-out too), purges scheduled
  tasks, revokes MCP tokens and deletes linked-URL fetches for the purged
  messages.
- "Forget everything you know about me" keeps its scope (facts), and its reply
  now points to `/privacy`.

Non-goals:

- The web version of the dashboard. It comes with
  `add-account-provisioning-and-user-area` and reuses the same service.
- Deleting the CyberdyneAuth account (an open external dependency).
- Changing `media_usage` retention (kept by the 0025 decision).

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
  - `erasure_request`;
  - `trace_export.asker_platform_user_id` plus an index;
  - a `scheduled_task` opt-out purge trigger;
  - opt-out handling of `mcp_token` and `document_fetch`.
- `adapters/tracing/langfuse.py`: record the asker. `composition.py`: build the
  deleter with Edges.
- `app/optout.py` and `admin/handlers/optouts.py`: trace withdrawal.
- Discord: the `/privacy` command, views, the modal. `self_description.py` and
  the commands snapshot.
- `docs/operations.md`: the opt-out gap closed, erasure semantics.
