Depends on `add-platform-ports`, including PR-P7 (linking, delivery routing,
personal documents, secret-material guard, `SpeechToText` port). Every PR adds regression tests for what it
guards, updates `docs/whatsapp-setup.md` / `docs/operations.md`, and ships dark
behind `ENABLED_PLATFORMS` not containing `whatsapp`.

## 0. Before any code (blocking)

- [ ] 0.1 Written sign-off on the narrowed WhatsApp persona under Meta Terms 4.7 (owner named in the PR)
- [ ] 0.2 Choose direct Cloud API vs BSP; business verification of the portfolio; dedicated number vs coexistence
- [ ] 0.3 Draft and submit utility templates (`cf_alert_fired`, `cf_scheduled_ready`, `cf_asks_digest`, `cf_link_confirm`) in en_US and pt_BR
- [ ] 0.4 Decide proactive budget and `WHATSAPP_DAILY_PROACTIVE_CAP` (unique recipients per rolling 24 h)
- [ ] 0.5 Decide the Cloud API data-residency (local storage) region and whether `LINKED_CORPUS_ON_WHATSAPP` may be enabled; record the data flow (Meta as processor) in `docs/whatsapp-setup.md`
- [ ] 0.6 Verify BSUID-addressed sends (text, interactive, template) on the Cloud API with the test number; record whether a phone fallback is ever needed
- [ ] 0.7 Decide the STT backend (AminiLLM on-prem or external) as a data-flow item; until decided, W6.3 is blocked and voice notes get the fixed reply

## 1. PR-W1: Webhook and Graph client

- [ ] 1.1 `adapters/whatsapp/` skeleton, `WHATSAPP_CAPABILITIES`, settings and secrets registration
- [ ] 1.2 `webhook.py`: verify GET, HMAC POST, `metadata.phone_number_id` check, durable insert then 200, worker drains rows until `processed_at`, `entrypoints/whatsapp.py` on the web server
- [ ] 1.3 Migration: `whatsapp_inbound` (wamid PK, `received_at`, `processed_at`, encrypted payload cleared once processed), `service_window`, `whatsapp_recipient` (encrypted), `pending_delivery`, `whatsapp_template`, `template_delivery` (cost, currency); `notification_preference.proactive_opt_in` with per-platform default; `ck_notification_outcome` gains `needs_template`, `not_opted_in`, `deferred`
- [ ] 1.4 `graph_client.py`: send text/interactive/template, media resolve/download, read+typing, 429/5xx retries, token redaction
- [ ] 1.5 Tests: forged signature, wrong verify token, duplicate wamid, restart after insert still answers once, payload for another `phone_number_id` ignored, inbound row holds no body, out-of-order replies quote their message

## 2. PR-W2: Identity and privacy

- [ ] 2.1 BSUID-only keying; payloads without BSUID counted and ignored; no merge by phone
- [ ] 2.2 Masking in logs, traces, admin console, MCP; egress guard E.164 check
- [ ] 2.3 Consented phone-fact offer with a confirm button
- [ ] 2.4 Opt-in/opt-out keywords stored in `notification_preference`
- [ ] 2.4a `forget everything`: `person_opt_out` purge triggers plus alerts, schedules, notifications, summaries, owner-only documents, `trace_export` rows and Langfuse traces, then WhatsApp identity, recipient, window, held deliveries, inbound rows; linked person: WhatsApp side only unless a second confirmation
- [ ] 2.5 Privacy regression suite: no phone/BSUID in logs, traces, prompts, stored display names; a recycled number with a new BSUID does NOT inherit the old person's data; facts, wallets, alerts and schedules are gone after forget everything; a linked person's Discord data survives unless they confirm

## 3. PR-W3: Assistant surface

- [ ] 3.1 `renderer.py` (markup, 4,096 split, tables as lists) and `inbound.py` into `ChatController`
- [ ] 3.2 `commands.py`: `/word` and localised keywords from `CommandCatalog`; two-level `menu` list message
- [ ] 3.3 `choices.py`: reply buttons and lists for `ChoicePrompt`; stale and foreign ids refused
- [ ] 3.4 `WHATSAPP_TOOLS` allowlist in the reasoning loop, at schedule creation and at every scheduled run; scope by deterministic routes plus one classification call; fixed out-of-scope reply
- [ ] 3.5 Disclosure on first contact and in help (Meta as processor of team content when `LINKED_CORPUS_ON_WHATSAPP`); secret-material guard enabled for WhatsApp
- [ ] 3.6 Capability-driven self-description; channel features refused with a fixed reply
- [ ] 3.7 Tests: channel catch-up refused, out-of-scope question makes no reasoning-loop run, out-of-scope schedule refused, run with a removed tool skipped, foreign button, menu ≤10 rows per level and every command reachable, pasted tx hash not refused

## 4. PR-W4: Window-aware delivery

- [ ] 4.1 `app/delivery_policy.py` (window and opt-in, from capabilities); `WhatsAppDirectMessenger` sends via `whatsapp_recipient` and templates only; `NEEDS_TEMPLATE` result
- [ ] 4.2 `templates.py` registry from `WHATSAPP_TEMPLATES`; language fallback; status and category webhooks disable templates, hold content, alert the admin
- [ ] 4.3 Held content, quick-reply tap delivers it, 7-day expiry
- [ ] 4.4 Alerts and schedules through the policy; ask digest (daily, linked people only)
- [ ] 4.5 Cap by unique recipients per rolling 24 h with alert priority; template cost with currency and window state in traces and admin console
- [ ] 4.6 Tests: out-of-window alert picks template, alert for a BSUID-only person delivered, tap on "Show"/"Ver" delivers full answer, no opt-in sends nothing (`not_opted_in`), cap defers digests first, paused template holds content, template moved to marketing disabled, cost recorded in BRL

## 5. PR-W5: Identity linking on WhatsApp

The linking flow itself is `add-platform-ports` PR-P7; this PR wires WhatsApp into it.

- [ ] 5.1 `link`/`vincular` and `unlink` keywords into `app/identity_linking.py`; issuing-platform prompt shows only "…3f"
- [ ] 5.2 `LINKED_CORPUS_ON_WHATSAPP` (default false) gating corpus answers and ask digest content; audited when enabled
- [ ] 5.3 Linked viewer (union) for WhatsApp; plain-text citations
- [ ] 5.4 Delivery routing honours a preferred WhatsApp delivery platform under window and opt-in
- [ ] 5.5 e2e tests (multi-wire, not only unit): link code issued on DiscordWire and redeemed on WhatsAppWire with confirmation on Discord; code in a channel refused; one code guessed from several WhatsApp identities invalidated; global failure ceiling alerts the operator; no link without issuing-platform confirmation; access revoked on source platform reflected; linked corpus refused while the setting is off; unlink

## 6. PR-W6: Media and voice

- [ ] 6.1 `media.py`: immediate download, one re-resolve on expiry, deletion after processing
- [ ] 6.2 Personal documents to the existing pipeline as owner-only entries (`owner_person_id`)
- [ ] 6.3 Voice notes through the `SpeechToText` port of `add-platform-ports`; fixed reply with the null transcriber. Wiring a real backend is **blocked on task 0.7**
- [ ] 6.4 Images: fixed reply
- [ ] 6.5 Tests: no STT backend reply and audio not stored; with a fake backend, voice note answered and audio deleted; another person never retrieves a personal document

## 7. PR-W7: End-to-end and docs

- [ ] 7.1 `WhatsAppWire`: signed webhook POSTs, Graph API captured with `httpx.MockTransport`, clock control for the window
- [ ] 7.2 Shared scenarios on WhatsApp (facts, wallets, alerts, schedules, privacy); channel scenarios visibly skipped
- [ ] 7.3 Live smoke test against Meta's test number (manual, documented)
- [ ] 7.4 `docs/whatsapp-setup.md`, README feature map

## 8. Later, optional: groups (needs an Official Business Account)

- [ ] 8.1 Business-created group as `ChannelRef`, ACL from group participant webhooks
- [ ] 8.2 Index from creation only; text confirmations; per-recipient cost accounting
- [ ] 8.3 Capabilities for group conversations expose catch-up within the group only
