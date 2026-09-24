## Why

People want the personal side of CyberFriend (wallets, DeFi positions,
alerts, scheduled answers, their own facts) where they already are, on their
phone, and for many of them that is WhatsApp rather than Discord or Slack.
WhatsApp is a different kind of platform, and its rules shape this change
more than the code does:

- **Policy first.** Section 4.7 ("AI Providers") of the Meta Terms for
  WhatsApp Business Platform bars providers of general-purpose AI assistants
  from the platform when AI is the *primary* functionality, at Meta's sole
  discretion (new users since 2025-10-15, everyone since 2026-01-15). AI that
  serves a specific business task remains allowed. CyberFriend on WhatsApp
  therefore has to be scoped and described as a crypto-portfolio, DeFi-alert
  and personal-organiser tool, with open-domain web Q&A off or narrowed.
  This is a product and legal decision that can block the whole track; the
  change makes it explicit and configurable rather than hiding it.
- **Only the Cloud API.** The On-Premises API stopped sending on 2025-10-23.
  Unofficial clients (whatsapp-web.js, Baileys) break the terms and risk bans.
- **1:1 only in practice.** There are no channels, no history API and no
  search API; the business sees only messages sent to it after onboarding.
  The 2026 Groups API exists but requires an Official Business Account, only
  covers groups the business creates (max 8 participants, invite links),
  cannot join existing groups and supports no buttons or lists. Corpus
  answers, catch-up, indexing and audience-aware channel answers are
  therefore unavailable on WhatsApp's own data.
- **Proactive messages are gated.** Free-form messages are allowed only in
  the 24-hour window after the person's last message; outside it only
  pre-approved templates (per language) may be sent, each delivered utility
  template is charged, and business-initiated sends need opt-in and are capped
  per portfolio (250 unique users per 24h until business verification).
- **Identity is a phone number or a BSUID.** Since April 2026 webhooks carry a
  business-scoped user id; since June 2026 username users may arrive with no
  phone number at all. Phone numbers are PII.
- **Meta decrypts messages** on its servers and keeps them for up to 30 days;
  user-side deletes produce no usable event.

## What Changes

- **`adapters/whatsapp/`** on the Cloud API (direct or via a BSP reselling the
  same API), behind `ENABLED_PLATFORMS` containing `whatsapp`:
  - `webhook.py`: GET verification (`hub.challenge` with the verify token),
    POST with `X-Hub-Signature-256` HMAC verification, fast 200 and async
    processing, dedup by `wamid`.
  - `inbound.py`: text, interactive replies, reactions, media, statuses;
    BSUID and phone extraction.
  - `graph_client.py`: messages, media, typing/read endpoints; retries; 429.
  - `renderer.py`: WhatsApp markup, 4,096-char chunks, tables as lists.
  - `choices.py`: reply buttons (max 3) and list messages (max 10 rows);
    text fallback outside the window.
  - `commands.py`: leading `/word` and localised keywords (`menu`, `ajuda`,
    `alertas`, `esquecer`, …) mapped to catalogue commands.
  - `media.py`: download within the 5-minute URL lifetime; documents to the
    existing pipeline; voice notes to the speech-to-text port.
  - `templates.py`: registry of approved utility templates per purpose and
    language.
- **Window-aware delivery**: a `service_window` per person, and a
  `DeliveryPolicy` inside `WhatsAppDirectMessenger` that sends free-form in
  the window and a utility template with a quick-reply button outside it,
  sending the full content when the person taps.
- **Opt-in** per person per platform, recorded, required for proactive
  messages, revocable with `stop`/`parar`.
- **Scope and disclosure**: a per-platform tool allowlist
  (`WHATSAPP_TOOLS`), a WhatsApp persona and self-description that state what
  the assistant is for and that Meta processes messages.
- **Identity and privacy**: people keyed by BSUID (phone kept only as a
  masked, consented fact); phone and BSUID never in logs, traces, prompts or
  the admin console in clear.
- **Identity linking**: a one-time code issued on one platform and redeemed on
  another links two platform identities to one person, which is the only way a
  WhatsApp user reaches their Discord or Slack corpus.
- **Voice notes** transcribed through the `SpeechToText` port introduced by
  the voice-transcription change (feature 10); without it, a fixed reply.
- **Feature matrix**: capabilities hide channels, catch-up, indexing and
  audience features from self-description and command hints on WhatsApp.
- **Groups**: specified as a later, optional phase requiring an OBA; off in
  this change.
- **e2e**: a `WhatsAppWire` posting signed webhooks and capturing Graph API
  calls.

Non-goals:

- Unofficial WhatsApp clients or reading a person's other chats.
- Open-domain "ask me anything" on WhatsApp.
- Marketing templates or any promotional message.
- Joining or reading user-created WhatsApp groups (not possible).
- Payments through WhatsApp.

## Capabilities

### New Capabilities

- `whatsapp-transport`: platform choice, webhook authentication, dedup,
  media, messaging limits and the 24-hour window with templates.
- `whatsapp-assistant`: the feature matrix on WhatsApp, command UX without
  slash commands, confirmations, proactive delivery of alerts, schedules and
  notifications, scope and disclosure, voice notes, and groups' status.
- `whatsapp-identity-privacy`: phone and BSUID identity, PII handling,
  opt-in, linking to an existing person, and forgetting.

### Modified Capabilities

None. Existing capabilities keep their requirements; the ones WhatsApp cannot
offer are hidden by the capability matrix of `add-platform-ports`.

## Impact

- Depends on `add-platform-ports`. Voice notes depend on the
  voice-transcription change; everything else does not.
- New: `src/chatmemory/adapters/whatsapp/*`, `entrypoints/whatsapp.py`,
  `app/identity_linking.py`, `app/delivery_policy.py`,
  `tests/e2e/harness/whatsapp_wire.py`, `docs/whatsapp-setup.md`.
- Migration: `whatsapp_inbound` (wamid dedup), `service_window`,
  `messaging_consent`, `identity_link_code`, `person_link`.
- Settings: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
  `WHATSAPP_WABA_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`,
  `WHATSAPP_TEMPLATES`, `WHATSAPP_TOOLS`, `WHATSAPP_DAILY_PROACTIVE_CAP`,
  `WHATSAPP_GROUPS` (default false).
- A public HTTPS endpoint on Coolify.
- Tracing gains window state and template cost attributes.

## Risk

The largest risk is not technical: Meta may judge the assistant general
purpose and ban the number. The mitigations are a narrow persona, the tool
allowlist, no marketing content, and a written sign-off before go-live
(task 0.1). The second risk is privacy: the platform id is a phone number, so
every place the core used to print an id is a leak; `add-platform-ports`
removes those, and this change adds regression tests that fail if a phone
number or BSUID reaches a log, trace, prompt or egress query. The third is
cost and reputation: unbounded proactive templates cost money and blocks
lower the quality rating, so proactive sends are opt-in, capped and batched
where possible.
