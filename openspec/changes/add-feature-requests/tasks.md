## 1. Store and commands

- [ ] 1.1 Migration 0028 `feature_request` + opt-out purge trigger + insert guard
- [ ] 1.2 Port + Postgres adapter; `FeatureRequestService` (submit, list_own, rate limit, dedupe, contact refusal)
- [ ] 1.3 `/suggest` and `/suggestions` in the command table; update `commands.json` snapshot
- [ ] 1.4 Acknowledgement with notify [Yes]/[No]
- [ ] 1.5 Tests: duplicate is idempotent; email/phone/wallet refused; 6th in 24h refused; opted-out refused; opt-out purges
- [ ] 1.6 e2e: FakeDiscord `/suggest` then `/suggestions` against real Postgres

## 2. Natural language

- [ ] 2.1 `suggestion_intent` (PT/EN markers, deferral to fact/alert/catch-up/obligation)
- [ ] 2.2 Dispatch after typed_command, before resolve_viewer; deferral lists in said_by and routing
- [ ] 2.3 Proposal with [Record suggestion]/[No, answer it] in a RequesterOnlyView
- [ ] 2.4 Routing eval cases: "sugiro que você me diga o preço do BTC" answers; "I'd like a feature that notifies me..." proposes suggestion, not alert
- [ ] 2.5 e2e: proposal, confirm stores; decline answers

## 3. Triage and notification

- [ ] 3.1 `GET /api/feature-requests` (operator), `PATCH /api/feature-requests/{id}` (admin, audited)
- [ ] 3.2 Svelte `FeatureRequestsVM` + screen (filters by status, duplicate count)
- [ ] 3.3 Status-change DM sweep in the bot process
- [ ] 3.4 Tests: operator PATCH -> 403; audit entry; DM sent once per status change; undeliverable skipped
- [ ] 3.5 Docs: user-facing commands, admin console screen
