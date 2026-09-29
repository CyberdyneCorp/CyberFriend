## 1. Storage

- [x] 1.1 Migration 0037: `person_secret` (ciphertext, last4, one row per person and kind), opt-out insert trigger, `purge_person_derived` restated with its DELETE, downgrade restores 0036's body (re-chained after 0036)
- [x] 1.2 `PERSONAL_SECRETS_KEY` setting (32 bytes base64, bot only, compose declaration); unusable key logs an error and turns the feature off
- [x] 1.3 `PostgresPersonalKeyStore`: AES-GCM with `person_secret:<person id>:<kind>` as associated data; unreadable row reads as no key
- [x] 1.4 Tests (Postgres): sealed at rest, owner-only read, copied ciphertext does not open, rotated key reads nothing, forget, opt-out purge and refusal, `purge_person_derived`, downgrade

## 2. Taking a key

- [x] 2.1 `find_key` (CyberWealth's shape, exactly one) and `mentions_key` (`cwk_` anywhere); `PersonalKeys.connect` only from a DM
- [x] 2.2 DM message carrying a key: stored, answered by the last four characters, never reaches the answer path; `/connect` registered in DMs only
- [x] 2.3 Channel message carrying `cwk_`: not stored, not answered, author warned by DM (sent or edited in); ingest withholds it (live, edit, backfill)
- [x] 2.4 `/ask`, `/suggest`, `/schedule create` with a key: never asked or stored
- [x] 2.5 Tests: shapes, replies never repeat the key, DM and channel handling, commands, archive withholding

## 3. Using a key

- [x] 3.1 `Federation.call(bearer=...)` over a per-call keyed connection; `KEY_REJECTED` on 401 without marking the server lost; `NO_PERSONAL_KEY`
- [x] 3.2 `GuardedInvoker` looks the key up for personal tools on key servers only, from the requester
- [x] 3.3 `register`: an allowlisted `my_` tool the service principal cannot list is registered for key servers
- [x] 3.4 `with_personal_keys`: on for a configured `cyberwealth` server over HTTPS only
- [x] 3.5 Tests over a real MCP server (ASGI): the key reaches only its owner's personal call, `intel_*` keeps the service token, no key sends nothing, a revoked key is reported and costs nobody else

## 4. Deletion and privacy

- [x] 4.1 `/forget` everywhere or in a DM deletes the key and says so
- [x] 4.2 `/privacy` lists keys (service, last four, date) in a DM and counts them in a channel; erasure reply counts them
- [x] 4.3 e2e: DM key sealed, listed, forgotten; channel key not archived, not answered, author warned

## 5. The key as a personal fact

- [x] 5.1 `PersonalKeyStore.held` (last four and date, never the ciphertext) and `forget(person, service)`; `HELD_KEY_OF_REQUESTER`, `FORGET_KEY` in the SQL audit
- [x] 5.2 `fact_intent`: `SHOW_KEY` / `FORGET_KEY` in English and Portuguese, before the fact patterns in the same precedence
- [x] 5.3 Fact listing in a DM carries the key by its last four characters; a channel listing never looks it up; one-key replies and the forget reply in the asker's language; forgetting every fact deletes it too
- [x] 5.4 Capabilities text names it (EN/PT), its examples route
- [x] 5.5 Tests: routing, masking, SHOW built without opening the key, store `held`/`forget`, e2e listed in a DM and not in a channel, forgotten in words keeping the facts, replaced

## 6. Docs

- [x] 6.1 README (feature, `/connect`, `PERSONAL_SECRETS_KEY`), `docs/operations.md` (a person's own key), roadmap
