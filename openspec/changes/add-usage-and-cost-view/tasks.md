## 1. Ops

- [ ] 1.1 (Done in add-privacy-dashboard 1.2) `LangfuseTraceDeleter` built with `edges.http_transport`
- [ ] 1.2 Ops: pin `langfuse/langfuse:3.225.8` and the worker tag in the Coolify `cyberfriend-langfuse` service; record the tags and the v4 trap in `docs/operations.md`; startup warning when `/api/public/health` reports a major version other than 3 (docs and warning done; applying the pin in Coolify is the remaining ops step)
- [ ] 1.3 Ops: limit Langfuse UI logins to the console admin set; document in `docs/operations.md` (documented; applying it in Coolify is the remaining ops step)

## 2. What traces carry

- [x] 2.1 `RunRecord.feature` set by the router/answer service; test that every route decision maps to a feature
- [x] 2.2 Trace name = feature; tags `app:cyberfriend`, feature/path/lang/tool; `LANGFUSE_ENVIRONMENT` setting
- [x] 2.3 Evidence exported as references only (window id, channel, source, score); test that no evidence text or excerpt is in the batch
- [x] 2.4 Move the tracer seam to the outermost answer service; capabilities/obligations/decisions traced; catch-up/said-by (answered before that chain) traced through the same tracer
- [x] 2.5 `ModelUsage` in `BudgetLedger`; thread completion tokens and model through `Plan`/`Grounded`
- [x] 2.6 `generation-create` per model call; `span-create` per federated tool call, no arguments
- [x] 2.7 `scripts/langfuse_models.py` + checked-in price table; docs
- [x] 2.8 Tests: batch shape, no tool arguments exported, opted-out asker still not exported (batch shape without generations/spans and opted-out asker done with 2.1-2.4; generation/span shape and tool arguments land with 2.5-2.6)

## 3. Retention

- [x] 3.1 `TRACE_RETENTION_DAYS` (90); daily sweep marking old `trace_export` rows
- [x] 3.2 Langfuse backstop scoped to our environment and `app:cyberfriend` tag (or our trace names for untagged traces from before the tag shipped, with a Discord user id and no other `app:` tag)
- [x] 3.3 Tests: rows past the cutoff become pending; backstop deletes an unindexed old trace; a foreign-environment trace, a foreign-name trace and another app's `fixed`/`loop` trace in our environment survive

## 4. Disclosure notice (after /privacy delete-everything ships)

- [x] 4.1 Migration: `person.tracing_notice_version`, `person.tracing_notice_at`
- [x] 4.2 One-time EN/PT notice appended to the first reply (DM or channel) per person and version; recorded with the export (claimed right after the traced answer by a conditional UPDATE that refuses opted-out people, rather than in the export's own transaction; see design)
- [x] 4.3 Capabilities reply states recording of questions and answers, 90 days, admin visibility, `/privacy`
- [x] 4.4 Tests: notice shown once per version; not shown to opted-out people or when tracing is off; PT and EN texts; capabilities snapshot

## 5. Admin API (live Langfuse reads)

- [ ] 5.1 `ports/usage.py` `UsageSource`; `adapters/tracing/langfuse_usage.py` (metrics + traces, admin transport, env and tag filters, row-limit split)
- [ ] 5.2 `UsageExclusions` from Postgres on every request (opt-out, open erasure, completed erasure up to `erased_before`, deletion-requested trace ids)
- [ ] 5.3 `GET /api/usage/summary` (operator), window cap 90 days, 5-minute in-process cache of pre-exclusion rows, 503 "usage unavailable", names joined at read time, voice from `media_usage` + anonymous total
- [ ] 5.4 `GET /api/usage/people/{id}/questions` (`admin_oidc`), trimmed fields, only traces after the person's notice, page size 50, no-store, audited per `sub` with the viewed person looked up
- [ ] 5.5 Tests: operator 403 and `cfa_` token 403 on questions; response never contains answer/evidence keys; audit entry names the viewer sub and viewed person; opted-out, erased and pending-deletion traces absent from counts and text (trace still present in FakeLangfuse); traces before the notice counted but not shown; Langfuse down -> 503

## 6. Screen

- [ ] 6.1 Svelte `UsageVM` + Usage screen; text section only for OIDC admin; "traced question runs" label; "usage unavailable" state
- [ ] 6.2 e2e: FakeLangfuse behind the admin transport; operator vs admin views
- [ ] 6.3 Docs: `docs/admin-console.md`, `docs/operations.md` (Langfuse keys rating for the admin process)
