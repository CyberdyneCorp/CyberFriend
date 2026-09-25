## 1. Store and commands

- [x] 1.1 Migration 0028 `feature_request`; add its delete to `purge_person_derived`; insert guard for opted-out people
  (shipped as 0030, chained after 0029; it adds `DELETE FROM feature_request` to `purge_person_derived` from 0028, with no trigger of its own; downgrade restores the 0028 body)
- [x] 1.2 Port + Postgres adapter; `FeatureRequestService` (submit, list_own, rate limit, dedupe, contact refusal)
- [x] 1.3 `/suggest` and `/suggestions` in the command table; update `commands.json` snapshot
- [x] 1.4 Acknowledgement with notify [Yes]/[No]
- [x] 1.5 Tests: duplicate is idempotent; email/phone/wallet refused; 6th in 24h refused (also under parallel submits); opted-out refused; opt-out purge and person-row cascade
  (erasure through `purge_person_derived` is covered too)
- [x] 1.6 e2e: FakeDiscord `/suggest` then `/suggestions` against real Postgres

## 2. Natural language

- [ ] 2.1 `suggestion_intent`: the six explicit forms only (PT/EN), matched at the start of the message
- [ ] 2.2 Dispatch as the last step before the default corpus answer; defer when the router's decision on the remainder is not the default
- [ ] 2.3 Proposal with [Record suggestion]/[No, answer it] in a RequesterOnlyView; timeout answers the message like "No, answer it"
- [ ] 2.4 Routing eval cases: "qual foi a sugestão do João?" not proposed; "you should be able to tell me X" not proposed; "sugestão: me diga o preço do BTC" answers the price; "it would be nice if you could show my portfolio" answers the portfolio; "feature request: notify me when BTC hits 100k" is an alert proposal; "tenho uma sugestão: avisar quando alguém me marcar" proposes a suggestion
- [ ] 2.5 e2e: proposal, confirm stores; decline answers; timeout answers

## 3. Triage and notification

- [ ] 3.1 `GET /api/feature-requests` (operator), `PATCH /api/feature-requests/{id}` (admin, audited); rows in the route-to-role table
- [ ] 3.2 Svelte `FeatureRequestsVM` + screen (filters by status, duplicate count)
- [ ] 3.3 Status-change DM sweep in the bot process (opt-in only)
- [ ] 3.4 Tests: operator PATCH -> 403; audit entry; DM sent once per status change; undeliverable skipped; no DM without opt-in
- [ ] 3.5 Docs: user-facing commands, admin console screen
