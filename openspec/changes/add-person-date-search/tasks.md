## PR 0. thread_context author ids

- [x] 0.1 `THREAD_CONTEXT` resolves the author's platform account; `HybridSearch.thread_context` builds the ref from it
- [x] 0.2 Regression test on real Postgres through the MCP tool, with a Discord id distinct from the person row id

## PR 1. Time spans

- [x] 1.1 `app/timespan.py`: `parse_span(text, now, tz)`, PT and EN, accent-insensitive, longest phrase first
- [x] 1.2 Calendar weeks (Monday first) and months, last N days, since a weekday, named months, dd/mm, ISO dates, "dia N"
- [x] 1.3 `ANSWER_TIMEZONE` setting, validated, default `America/Sao_Paulo`, declared for the bot in docker-compose.yml
- [x] 1.4 Unit tests: fixed clock, 23:30 BRT, DST-free Sao Paulo, week and month boundaries, year rollover
- [x] 1.5 README and deploy-coolify.md document the setting

## PR 2. Author-scoped retrieval and resolution

- [x] 2.1 Migration: `ix_message_author_time` partial index
- [x] 2.2 `HybridSearch.search` author branch: ACL, tombstones, author, bounds in one statement; exact cosine ranking; author-only hits
- [x] 2.3 `SearchHit.author_display`; `SearchBackend.people_named` with the visible-message predicate; fakes implement it
- [x] 2.4 Integration tests: ACL, tombstones, bounds, multi-author windows, `people_named` scoping, statement audit

## PR 3. The route

- [x] 3.1 `said_by_request` parser with precedence against obligations, catch-up, market and facts
- [x] 3.2 `SaidByService` dispatched after catch-up; provenance `said_by` resolved/ambiguous/fallback
- [x] 3.3 Ambiguity reply with zero model calls; uniform empty reply; `RetrievalUnavailable` answered as a failure
- [x] 3.4 Labelled PT/EN eval set including non-person subjects
- [x] 3.5 E2E: the scenarios in the spec
- [x] 3.6 README, docs/operations.md, self-description
- [x] 3.8 Fix: recognise who it was said to ("falou com você", "te falou", "told you")
- [x] 3.9 Fix: name people stored under their account id when ingest connects
- [ ] 3.7 Archive the change once merged
