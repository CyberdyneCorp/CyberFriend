## 1. Regression fixes (failing tests first)

- [x] 1.1 Migration: `purge_person_derived(person_id)` consolidating the 0013/0014/0015/0020 trigger bodies, plus `scheduled_task` and `mcp_token`; the `person_opt_out` trigger calls it; due sweep skips opted-out people
- [x] 1.2 Test: an opted-out person's scheduled task is deleted and never runs (real Postgres); existing opt-out purge tests still pass
- [x] 1.3 Test (e2e): trace deletion reaches FakeWeb -> build `LangfuseTraceDeleter` with `edges.http_transport`
- [x] 1.4 Test: opt-out revokes the person's MCP tokens and deletes `document_fetch` rows for purged messages

## 2. Traces by asker

- [ ] 2.1 Migration: `trace_export.asker_platform_user_id` + index; written by `record_export`
- [ ] 2.2 `TraceIndex.request_deletion_for_asker(ids)`
- [ ] 2.3 Langfuse backstop: page traces by `userId` scoped to our environment and trace names, insert pending ids (ingest process)
- [ ] 2.4 `OptOutService` withdraws quoted and asked traces (admin opt-out too)
- [ ] 2.5 Tests: asked and quoting traces marked; unindexed trace found by backstop; foreign-environment trace untouched; v4-style 400 keeps it pending

## 3. `/privacy` dashboard (after trace retention ships)

- [ ] 3.1 `PrivacyService.inventory(person)` across all stores in the design table, including `message_media`
- [ ] 3.2 Guild view (ephemeral, counts/kinds) and DM view (embeds, pages)
- [ ] 3.3 Archive coverage from `ChannelListingService`; unreadable channels neither named nor counted
- [ ] 3.4 Fixed statements: questions and answers recorded up to 90 days, admins can read them; the kept list
- [ ] 3.5 Command table, `commands.json` snapshot; "forget everything you know about me" reply points to `/privacy`
- [ ] 3.6 Tests: DIRECT_ONLY_KINDS values never in guild reply; no hidden channel names or counts

## 4. Delete everything

- [ ] 4.1 Migrations: `erasure_request` (with `mode`), `person.erased_before`, `media_usage_anonymous`; the monthly ceiling sums both usage tables
- [ ] 4.2 Backfill and ingestion skip a person's messages created before `erased_before`
- [ ] 4.3 `PrivacyService.erase(person, mode)` steps 1-8, idempotent, with step tracking; purge via `OptOutService` message purge + `purge_person_derived`
- [ ] 4.4 Resume sweep in ingest
- [ ] 4.5 [Delete everything...] view with the kept list and two buttons, then the typed-confirmation modal (DELETE/APAGAR), requester only, timeout
- [ ] 4.6 Tests: crash after step 4 resumes; tombstone keeps id, platform ids, `erased_before` and (mode 2) the opt-out flag; backfill does not re-import in either mode; mode 1 archives a new message afterwards; monthly ceiling unchanged after the fold
- [ ] 4.7 e2e: FakeDiscord `/privacy` -> delete (each mode) -> counts reply; FakeWeb receives trace DELETE; real Postgres has no `message`, `message_media`, `media_usage`, `scheduled_task`, `person_fact`, `conversation_turn`, `mcp_token` or `document_fetch` rows for the person
- [ ] 4.8 Ops: verify Langfuse v3 deletion removes MinIO event blobs; document
- [ ] 4.9 Docs: `docs/operations.md` opt-out gap closed; erasure semantics; the kept list; Postgres backup retention; which processes hold the Langfuse key pair
