## 1. Storage (PR-A1)

- [x] 1.1 Migration 0020: `position_alert`, keyed by person, cascading on deletion
- [x] 1.2 Kind, chain, address, language, state and threshold bounded in the schema
- [x] 1.3 A per-kind check that each kind carries exactly its target columns
- [x] 1.4 Partial due index, owner index, partial unique index against duplicates
- [x] 1.5 Cap of 10 active alerts enforced inside the insert; duplicates skipped by `ON CONFLICT`
- [x] 1.6 Claim with `FOR UPDATE SKIP LOCKED`, advancing before the read
- [x] 1.7 Opt-out purges and refuses; forgetting or replacing the saved wallet purges its alerts
- [x] 1.8 Test: cap, duplicates, ownership, claim once, disable, record, cascade, opt-out, forget

## 2. Deciding (PR-A1)

- [x] 2.1 `ports/alerts.py`: kinds, states, alert, readings, store and observer protocols
- [x] 2.2 Pure `evaluate` with confirmation, hysteresis, closed-once and failure rules
- [x] 2.3 English and Portuguese message templates
- [x] 2.4 `AlertService` with the threshold bounds and target checks
- [x] 2.5 Test: every transition, both languages, the bounds

## 3. Reading the chain (PR-A1)

- [x] 3.1 `ChainWatcher`: one `aggregate3` per chain for pinned v3, v4 and Aave reads
- [x] 3.2 Its own rate limiter, a per-chain call cap, the key redacted
- [x] 3.3 Through `Edges.http_transport`
- [x] 3.4 Test: in range, out of range, closed (burned, transferred, withdrawn), HF and no debt, one request per chain, a failing chain

## 4. Running (PR-A1)

- [x] 4.1 `AlertRunner`: claim, read in one batch, evaluate, send, record
- [x] 4.2 Closed direct messages disable every alert of that person
- [x] 4.3 `alert_loop` in the bot beside the scheduled sweep, on the edges' clock, after the gateway
- [x] 4.4 `ALERTS_ENABLED` (default false) and `ALERT_SWEEP_SECONDS` (default 300, floor 60), declared in compose
- [x] 4.5 Test: nothing built by default; built and started when on
- [x] 4.6 End-to-end: a range alert messages once out and once back in, in its language; a health alert fires once, holds in the band, stays silent on failure and recovers

## 5. The surface (PR-A2)

- [x] 5.1 Natural-language creation with a confirm button; the address checked at creation
- [x] 5.2 Baseline read at creation, shown in the reply; one alert per open position
- [x] 5.3 `/alert list` and `/alert delete`, the same refusal for "not yours" and "no such alert"
- [x] 5.4 Messages name `/alert list` and `/alert delete`
- [x] 5.5 Docs updated; the default stays off and the operator sets `ALERTS_ENABLED=true` at deploy
- [x] 5.6 `ALERT_CREATE` recognised before retrieval and the positions routes; conversation verbs veto; a reading ("tell me if my HF is ok") is not a request
- [x] 5.7 With alerts off or no Infura key, a request is told alerts are not available
- [x] 5.8 The wallet shown only in a direct message; the prompt ephemeral for `/ask`
- [x] 5.9 `/alert list|delete` global, in the capability reply where alerts are on, in the anti-drift test and the command snapshot
- [x] 5.10 Test: recogniser both directions (EN/PT, typos, negatives); proposal rules over a scripted read; creation read over the scripted node; the prompt's expiry and the listing
- [x] 5.11 End-to-end: a Portuguese request confirmed, stored and later messaged; Cancel and another person's press create nothing; `/ask`, `/alert list` and `/alert delete`; a limit out of bounds; alerts off
