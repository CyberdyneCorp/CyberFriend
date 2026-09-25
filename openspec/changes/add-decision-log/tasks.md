## PR 5. Extraction and storage

- [x] 5.1 Migration 0024: `decision` table, stable `decision_key`, source FK with cascade, denormalised channel, evidence array, nullable embedding, `'simple'` tsvector
- [x] 5.2 `OUTPUT_SCHEMA` `{asks, decisions}`, both required; DECISION section in `SYSTEM_PROMPT`; `parse_extraction` / `parse_decisions`
- [x] 5.3 `AskExtractor.extract` returns `Extraction(asks, decisions)`; adapter and fakes updated
- [x] 5.4 `AddresseeSignal.DECISION` with bilingual `DECISION_MARKERS`
- [x] 5.5 `PostgresDecisionStore`: embed at write time (NULL on failure), upsert then prune per source, withdraw non-candidates
- [x] 5.6 Retention (`PURGE_DECISIONS_BEFORE`) and opt-out (`PURGE_PERSON_DECISIONS`, evidence authors included); counts in `CorpusPurge`, `PersonPurge` and the admin response
- [x] 5.7 PT/EN decision eval set beside the ask set, precision gate 0.9; the ask set still passes against gpt-4o-mini
- [x] 5.8 Unit, integration (idempotency, edit withdrawal, cascade, retention, opt-out) and e2e (a PT decision is stored, a proposal is not)
- [x] 5.9 Backlog pass reads an edited message's conversation from the corpus (`Store.extraction_context`); a chunk whose conversation cannot be read is retried
- [x] 5.10 Deleting a message (tombstone) withdraws every decision whose evidence holds it; GIN index on `evidence_message_ids`
- [x] 5.11 Markers narrowed: "fechada", "vamos de", "going with" dropped; candidate rate and prompt-token cost recorded in design.md

## PR 6. The answer

- [x] 6.1 `decision_question(text)`: PT/EN, topic and period, `_NOT_A_LOOKUP` guard
- [x] 6.2 `DecisionAnswerService` after `ObligationAnswerService`, under `retrieval_viewer`
- [x] 6.3 Search: ACL in WHERE, source and evidence alive, confidence and period filters, exact `0.7*cosine + 0.3*ts_rank`; `statements()` audit
- [x] 6.4 Deterministic, localised, dated, cited reply; fall through below the similarity floor
- [x] 6.5 E2E: PT and EN answers, no leak across channels, deleted source or proposal not reported, fall-through, zero chat-model calls
- [x] 6.6 `DECISION_MIN_SIMILARITY` setting; README, operations and deploy docs; capability examples route to the decision path

## PR 7. Backfill

- [x] 7.1 `just decisions-backfill --since`, resetting the watermark only for live marker-bearing messages in indexing scope; markers matched in Python with the filter's own pattern
- [x] 7.2 docs/operations.md, including that asks on those messages are re-extracted (keys, statuses and corrections kept)
- [x] 7.3 Unit, integration (window, scope, tombstone, markers; ask keys, statuses and corrections survive re-extraction; a reclassified ask comes back open) and e2e (pre-feature history yields a cited decision after backfill and a drain)
- [x] 7.3a `--until` to stop the window where decision extraction began; the command's own stored-scope read and its refusal tested through `run()`; the e2e drain built over `ScopedExtractionLedger` as ingest builds it
- [ ] 7.4 Archive this change once PR 7 is merged
