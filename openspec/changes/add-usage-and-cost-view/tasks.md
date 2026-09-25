## 1. Hardening first

- [ ] 1.1 Build `LangfuseTraceDeleter` with `edges.http_transport` (regression test: e2e FakeWeb sees the DELETE)
- [ ] 1.2 Pin `langfuse/langfuse:3.225.8` and the worker in compose; document the v4 trap

## 2. What traces carry

- [ ] 2.1 `RunRecord.feature` set by the router/answer service; test that every route decision maps to a feature
- [ ] 2.2 Trace name = feature; tags feature/path/lang/tool
- [ ] 2.3 Move the tracer seam to the outermost answer service; capabilities/obligations/decisions traced
- [ ] 2.4 `ModelUsage` in `BudgetLedger`; thread completion tokens and model through `Plan`/`Grounded`
- [ ] 2.5 `generation-create` per model call; `span-create` per federated tool call, no arguments
- [ ] 2.6 `scripts/langfuse_models.py` + checked-in price table; docs
- [ ] 2.7 Tests: batch shape, no tool arguments exported, opted-out asker still not exported

## 3. Retention and disclosure

- [ ] 3.1 `TRACE_RETENTION_DAYS` (90); daily sweep marking old `trace_export` rows; Langfuse backstop query
- [ ] 3.2 Capabilities reply: admins can read your questions for 90 days; `/privacy` deletes them
- [ ] 3.3 Tests: rows past the cutoff become pending; backstop deletes an unindexed old trace

## 4. Rollup

- [ ] 4.1 Migration: `usage_rollup`, `usage_tool_rollup`, `usage_sync_state`
- [ ] 4.2 `ports/usage.py`; `adapters/tracing/langfuse_usage.py` (metrics + traces, Edges transport)
- [ ] 4.3 Sync loop in ingest (15 min, 48h recompute, split by day at row limit); health details
- [ ] 4.4 Rollup retention (13 months)
- [ ] 4.5 Tests: late write within 48h is picked up; frozen day unchanged; row-limit split

## 5. Admin API and screen

- [ ] 5.1 `GET /api/usage/summary` (operator), window cap 90 days, names joined at read time, voice from `media_usage`
- [ ] 5.2 `GET /api/usage/people/{id}/questions` (admin), trimmed fields, page size 50, no-store, audited
- [ ] 5.3 Tests: operator gets 403 on questions; response never contains answer/evidence keys; audit entry written
- [ ] 5.4 Svelte `UsageVM` + Usage screen; text section only for admin; "traced question runs" label + last sync
- [ ] 5.5 e2e: FakeLangfuse behind the admin transport; operator vs admin views
- [ ] 5.6 Docs: `docs/admin-console.md`, `docs/operations.md`
