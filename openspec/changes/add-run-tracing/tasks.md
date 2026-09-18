## 1. The port and the contract

- [x] 1.1 `RunTrace`: question, answer, record, evidence
- [x] 1.2 `RunTracer` port with a no-op default
- [x] 1.3 Carry `evidence` on `RunOutcome`, populated by `finish_run`
- [x] 1.4 Call the tracer from `ReasoningAnswerService._recorded`
- [x] 1.5 Skip the export when the asker has opted out
- [x] 1.6 Test: a run with no destination configured exports nothing
- [x] 1.7 Test: an abstained run still records question and cause
- [x] 1.8 Test: an opted-out asker is never exported

## 2. The Langfuse adapter

- [x] 2.1 Client for the ingestion API, with timeout and no retry on the answer path
- [x] 2.2 Map a `RunTrace` onto a trace with spans for retrieval and synthesis
- [x] 2.3 Record evidence with channel, source system, score and text
- [x] 2.4 Swallow and log every transport failure
- [x] 2.5 Test: an unreachable destination does not raise
- [x] 2.6 Test: a slow destination is abandoned at the timeout

## 3. Deletion propagation

- [x] 3.1 Migration: table mapping exported trace id to quoted message ids
- [x] 3.2 Record the mapping when a trace is exported
- [x] 3.3 Delete traces for a message from `IngestService.handle_delete`
- [x] 3.4 Keep failed deletions pending and retry them
- [x] 3.5 Corpus deletion succeeds even when the destination is unreachable
- [x] 3.6 Test: deleting a traced message deletes its trace
- [x] 3.7 Test: an unreachable destination does not block the tombstone

## 4. Settings, wiring and documentation

- [x] 4.1 Settings for destination, keys, enablement and timeout
- [x] 4.2 Declare every setting in `docker-compose.yml`
- [x] 4.3 Wire the tracer in `composition.py` and assert the call chain in a test
- [x] 4.4 README and `docs/operations.md`: what is exported and what that means

## 5. Live

- [ ] 5.1 Deploy with the destination configured
- [ ] 5.2 Confirm a real question appears as a trace with its evidence
