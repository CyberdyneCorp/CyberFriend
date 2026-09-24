Depends on `add-platform-ports`. Every PR adds regression tests for what it
guards, updates `docs/whatsapp-setup.md` / `docs/operations.md`, and ships dark
behind `ENABLED_PLATFORMS` not containing `whatsapp`.

## 0. Before any code (blocking)

- [ ] 0.1 Written sign-off on the narrowed WhatsApp persona under Meta Terms 4.7 (owner named in the PR)
- [ ] 0.2 Choose direct Cloud API vs BSP; business verification of the portfolio; dedicated number vs coexistence
- [ ] 0.3 Draft and submit utility templates (`cf_alert_fired`, `cf_scheduled_ready`, `cf_asks_digest`, `cf_link_confirm`) in en_US and pt_BR
- [ ] 0.4 Decide proactive budget and `WHATSAPP_DAILY_PROACTIVE_CAP`

## 1. PR-W1: Webhook and Graph client

- [ ] 1.1 `adapters/whatsapp/` skeleton, `WHATSAPP_CAPABILITIES`, settings and secrets registration
- [ ] 1.2 `webhook.py`: verify GET, HMAC POST, fast 200, `entrypoints/whatsapp.py` on the web server
- [ ] 1.3 Migration: `whatsapp_inbound` (wamid dedup), `service_window`, `messaging_consent`, `pending_delivery`
- [ ] 1.4 `graph_client.py`: send text/interactive/template, media resolve/download, read+typing, 429/5xx retries, token redaction
- [ ] 1.5 Tests: forged signature, wrong verify token, duplicate wamid, out-of-order replies quote their message

## 2. PR-W2: Identity and privacy

- [ ] 2.1 BSUID keying, peppered phone hash fallback, merge on first sighting of both
- [ ] 2.2 Masking in logs, traces, admin console, MCP; egress guard E.164 check
- [ ] 2.3 Consented phone-fact offer with a confirm button
- [ ] 2.4 Opt-in/opt-out keywords and storage
- [ ] 2.5 Privacy regression suite: no phone/BSUID in logs, traces, prompts, stored display names

## 3. PR-W3: Assistant surface

- [ ] 3.1 `renderer.py` (markup, 4,096 split, tables as lists) and `inbound.py` into `ChatController`
- [ ] 3.2 `commands.py`: `/word` and localised keywords from `CommandCatalog`; `menu` list message
- [ ] 3.3 `choices.py`: reply buttons and lists for `ChoicePrompt`; stale and foreign ids refused
- [ ] 3.4 `WHATSAPP_TOOLS` allowlist in the reasoning loop; fixed out-of-scope reply
- [ ] 3.5 Disclosure on first contact and in help; seed-phrase detection and non-storage
- [ ] 3.6 Capability-driven self-description; channel features refused with a fixed reply
- [ ] 3.7 Tests: channel catch-up refused, out-of-scope question makes no model call, foreign button, menu ≤10 rows

## 4. PR-W4: Window-aware delivery

- [ ] 4.1 `WhatsAppDirectMessenger` with the delivery policy; `NEEDS_TEMPLATE` result
- [ ] 4.2 `templates.py` registry from `WHATSAPP_TEMPLATES`; language fallback
- [ ] 4.3 Held content, quick-reply tap delivers it, 7-day expiry
- [ ] 4.4 Alerts and schedules through the policy; ask digest (daily, linked people only)
- [ ] 4.5 Daily cap with alert priority; template cost and window state in traces and admin console
- [ ] 4.6 Tests: out-of-window alert picks template, tap delivers full answer, no opt-in sends nothing, cap defers digests first

## 5. PR-W5: Identity linking

- [ ] 5.1 `app/identity_linking.py`: code issue in private conversations only, hashed storage, expiry, attempt limit
- [ ] 5.2 `person_link` and merge of facts, wallets, alerts, schedules with conflict prompts
- [ ] 5.3 Linked viewer resolution for WhatsApp; plain-text citations
- [ ] 5.4 `unlink` on both sides
- [ ] 5.5 Tests: code in a channel refused, brute force locked, access revoked on source platform reflected, unlink

## 6. PR-W6: Media and voice

- [ ] 6.1 `media.py`: immediate download, one re-resolve on expiry, deletion after processing
- [ ] 6.2 Personal documents to the existing pipeline, scoped to the person
- [ ] 6.3 Voice notes through the `SpeechToText` port of the voice-transcription change (feature 10); fixed reply without it
- [ ] 6.4 Images: fixed reply
- [ ] 6.5 Tests: voice note answered and audio deleted, no STT backend reply

## 7. PR-W7: End-to-end and docs

- [ ] 7.1 `WhatsAppWire`: signed webhook POSTs, Graph API captured with `httpx.MockTransport`, clock control for the window
- [ ] 7.2 Shared scenarios on WhatsApp (facts, wallets, alerts, schedules, privacy); channel scenarios visibly skipped
- [ ] 7.3 Live smoke test against Meta's test number (manual, documented)
- [ ] 7.4 `docs/whatsapp-setup.md`, README feature map

## 8. Later, optional: groups (needs an Official Business Account)

- [ ] 8.1 Business-created group as `ChannelRef`, ACL from group participant webhooks
- [ ] 8.2 Index from creation only; text confirmations; per-recipient cost accounting
- [ ] 8.3 Capabilities for group conversations expose catch-up within the group only
