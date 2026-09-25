## 1. Regression fixes (failing tests first)

- [ ] 1.1 Test: an opted-out person's scheduled task is deleted and never runs -> trigger migration + sweep filter
- [ ] 1.2 Test (e2e): trace deletion reaches FakeWeb -> build `LangfuseTraceDeleter` with `edges.http_transport`
- [ ] 1.3 Test: opt-out revokes the person's MCP tokens and deletes `document_fetch` rows for purged messages

## 2. Traces by asker

- [ ] 2.1 Migration: `trace_export.asker_platform_user_id` + index; written by `record_export`
- [ ] 2.2 `TraceIndex.request_deletion_for_asker(ids)`
- [ ] 2.3 Langfuse backstop: page traces by `userId`, insert pending ids (ingest process)
- [ ] 2.4 `OptOutService` withdraws quoted and asked traces (admin opt-out too)
- [ ] 2.5 Tests: asked and quoting traces marked; unindexed trace found by backstop; v4-style 400 keeps it pending

## 3. `/privacy` dashboard

- [ ] 3.1 `PrivacyService.inventory(person)` across all stores in the design table
- [ ] 3.2 Guild view (ephemeral, counts/kinds) and DM view (embeds, pages)
- [ ] 3.3 Archive coverage from `ChannelListingService`; one aggregate for hidden channels
- [ ] 3.4 Fixed statements (tracing/admins/90 days, mentions kept, voice minutes kept)
- [ ] 3.5 "Download my data" JSON by DM
- [ ] 3.6 Command table, `commands.json` snapshot; "forget everything you know about me" reply points to `/privacy`
- [ ] 3.7 Tests: DIRECT_ONLY_KINDS values never in guild reply; no hidden channel names

## 4. Delete everything

- [ ] 4.1 Migration: `erasure_request`
- [ ] 4.2 `PrivacyService.erase` steps 1-6, idempotent, with step tracking
- [ ] 4.3 Resume sweep in ingest
- [ ] 4.4 Button + typed-confirmation modal (DELETE/APAGAR), requester only, timeout
- [ ] 4.5 Tests: crash after step 3 resumes; person row and opt-out flag kept; backfill does not re-import
- [ ] 4.6 e2e: FakeDiscord `/privacy` -> delete -> counts reply; FakeWeb receives trace DELETE; real Postgres empty for the person
- [ ] 4.7 Ops: verify Langfuse v3 deletion removes MinIO event blobs; document
- [ ] 4.8 Docs: `docs/operations.md` opt-out gap closed; erasure semantics
