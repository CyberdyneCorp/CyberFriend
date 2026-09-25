## PR 8. Capture voice notes and images as pending media

- [x] 8.1 `domain/media.py` (`MediaKind`, `MediaRef`, the allowlists); `Message.media`
- [x] 8.2 `source.media_of`: allowlisted declared type, Discord CDN URL, voice by `IS_VOICE_MESSAGE`; called by `to_message`
- [x] 8.3 Migration 0026 `message_media` (FK cascade, status, attempts, `UNIQUE(message_id, attachment_id)`, text only when done)
- [x] 8.4 `PostgresStore`: pending rows in the capture transaction from `media_since`, URL refreshed while pending, dropped attachments deleted, tombstone withdraws; SQL audit registrations
- [x] 8.5 Settings `MEDIA_ENABLED_AT`, `MEDIA_BACKFILL_DAYS`; `build_corpus_store` for ingest and the e2e harness; compose declares them for ingest only
- [x] 8.6 Tests: unit (`media_of`, settings), integration (capture, refresh, edit, delete-before-insert, opt-out trigger, tombstone, retention/opt-out/channel purge cascade), e2e through the gateway handler (DM, private thread, unindexed channel create no row; an indexed channel creates exactly one pending row; nothing fetched, no model call)
- [x] 8.7 docs/operations.md, docs/deploy-coolify.md

## PR 9. Transcribe voice notes into the windows

- [ ] 9.1 `MediaWorker` and the claim statement (scope array, opt-outs incl. per-person media opt-out, attempts, backoff, monthly audio budget shared with `media_usage`)
- [ ] 9.2 Download over the process transport from the CDN only; magic-byte sniffing; 403/404 as an attempt with backoff
- [ ] 9.3 Post-processing: contact withholding (spelled-out digits), secret redaction, hallucination filter, truncation
- [ ] 9.4 Rewindow aggregation and `_render` markers; settings and validators; docs/media-ingestion.md
- [ ] 9.5 Unit and e2e scenarios from the plan

## PR 10. Describe and read images

- [ ] 10.1 `ImageDescriber` and adapter (base64 data URL, fixed JSON prompt)
- [ ] 10.2 Separate switch and monthly image cap; `content_sha256` reuse
- [ ] 10.3 Unit and e2e scenarios from the plan; archive this change
