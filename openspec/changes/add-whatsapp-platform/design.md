## Principle

On WhatsApp, CyberFriend is a **1:1 personal assistant for a person's own
crypto positions, alerts, schedules and facts**. It is not a window onto team
channels, except through an identity the person has proven is theirs and
only where the operator has allowed team content to pass through Meta.

## Platform setup

Cloud API only (direct, or via a BSP such as Twilio or 360dialog that resells
it; the adapter talks Graph API shapes and a BSP is a base-URL and auth
change). Requires a Meta app, a Business portfolio, a WABA, a registered
number, a system-user permanent token and a webhook subscription (including
`message_template_status_update` and `template_category_update`). The Cloud
API local-storage (data residency) region is decided before go-live (task
0.5). Business
verification is recommended before real users: it lifts the proactive cap
from 250 unique users per 24h. A number already on the WhatsApp Business app
can be onboarded in coexistence mode; its 180-day history webhook is **not**
consumed (those are the number's own chats with third parties, and not ours to
index).

## Capability declaration

```python
WHATSAPP_CAPABILITIES = PlatformCapabilities(
    has_channels=False, has_threads=False, has_history_backfill=False,
    has_ephemeral=False, has_slash_menu=False, commands_in_threads=False,
    max_buttons=3, has_list_picker=True, edits_and_deletes_are_events=False,
    has_reactions=True, max_plain_chars=4096, max_rich_chars=4096,
    markup=MarkupDialect.WHATSAPP,
    proactive=ProactivePolicy.WINDOW_OR_TEMPLATE, has_permalinks=False,
)
```

Every chat is `ConversationKind.DM`; `AudienceResolver.resolve_private` is the
only audience; `AclResolver` returns an empty visible set unless the person is
linked and `LINKED_CORPUS_ON_WHATSAPP` is on (below). The progress indicator
declares `refresh_interval = 20 s`, because Meta drops the typing indicator
after 25 s.

## Inbound

```
Meta ──POST /whatsapp/webhook──▶ verify X-Hub-Signature-256 (HMAC-SHA256 of raw body,
                                  app secret, constant-time) ──▶ 200 within ~1 s
                                  └─▶ INSERT whatsapp_inbound(wamid, received_at) ON CONFLICT DO NOTHING
                                        └─▶ worker: rows WHERE processed_at IS NULL (SKIP LOCKED)
                                              ▶ parse ▶ window touch ▶ controller ▶ processed_at = now()
```

- `whatsapp_inbound` stores only `wamid`, `received_at` and `processed_at`;
  the message body lives only in the encrypted work payload
  (`payload_enc`), which is cleared once processed. A crash after the insert
  leaves the row unprocessed, and the worker processes it after restart, so a
  message is answered once, never zero times.
- A payload whose `metadata.phone_number_id` is not
  `WHATSAPP_PHONE_NUMBER_ID` is acknowledged and ignored.
- `messages[].type`: `text`, `interactive` (`button_reply.id`,
  `list_reply.id`), `button` (template quick reply), `reaction`, `audio`,
  `document`, `image`, `video`, `location`, `contacts`, `unsupported`.
- `statuses[]`: `sent/delivered/read/failed` update the delivery record and
  template cost; `failed` with a re-engagement error marks the window closed.
- Out-of-order delivery is expected; replies quote the triggering message via
  `context.message_id`.
- Read receipt and typing indicator are sent together as the progress
  indicator, re-sent every 20 s until the reply (Meta clears it after 25 s).
- `message_template_status_update` (paused, rejected, disabled) and
  `template_category_update` (for example utility to marketing) disable the
  template in the registry and alert the admin; sends of that purpose fall
  back to holding the content until the person's next inbound message.

## Identity

- Key: `PersonRef("whatsapp", <BSUID>)`, always. Every `messages` webhook has
  carried a BSUID since April 2026, and carriers recycle phone numbers, so a
  phone-derived key would hand a new owner of a recycled number the previous
  owner's facts, wallets, alerts and linked corpus. A payload without a BSUID
  is acknowledged, counted (masked) for the operator and not answered.
- No merge by phone: a new BSUID is a new person even when the phone number is
  one seen before. There is no phone-hash identity.
- The phone number (if the payload carries it) is not saved silently as a
  fact; the assistant may offer to save it as the person's phone fact, masked
  in every display except to its owner in the 1:1 chat.
- `contacts[].profile.name` is the display name; never the id.

## Delivery address

The send address is separate from identity and from the phone fact:

| `whatsapp_recipient` | |
|---|---|
| `person_id` | FK, unique per WhatsApp identity |
| `bsuid` | preferred send address |
| `phone_e164` | only when a payload supplied it; used only if BSUID-addressed sends are unsupported for that user |
| `updated_at` | last payload that confirmed the address |

Columns are encrypted at rest (the same key handling as `slack_install`
tokens), never logged, traced, shown in the admin console or exported. The
phone-as-fact consent flow never reads from or writes to this table. Task 0.6
verifies BSUID-addressed sends on the Cloud API before PR-W4.

## Linking to an existing person

Linking is the platform-neutral flow of `add-platform-ports`
(`platform-identity`: one-time code, confirmation on the issuing platform,
per-code and global failure caps, `person_platform_id` re-pointed, no link
table). WhatsApp-specific rules:

- The redeeming WhatsApp identity is shown on the issuing platform only by
  its last two BSUID characters ("WhatsApp account ending …3f").
- Linked corpus and ask content reach WhatsApp only when
  `LINKED_CORPUS_ON_WHATSAPP=true` (default `false`). When it is off, a linked
  person on WhatsApp still gets their facts, wallets, alerts and schedules,
  but no team-channel evidence and no ask digest content. When on, the
  WhatsApp disclosure names Meta as a processor of that content (Meta
  decrypts messages and may keep them up to 30 days), and the operator's
  enabling of it is audited.
- Answers stay private (1:1), and citations render as plain text names.

## Delivery policy

The window and opt-in policy is platform-neutral code in
`app/delivery_policy.py`, driven by `ProactivePolicy.WINDOW_OR_TEMPLATE` and
`service_window`; the WhatsApp adapter only renders and sends templates.

```
send_direct(person, text, purpose)
  ├─ proactive purpose and notification_preference not opted in ─▶ NOT SENT, outcome not_opted_in
  ├─ window open (last inbound < 24h, margin 15 min) ─▶ free-form text/interactive
  ├─ window closed, template active ─▶ template[purpose][language], quick reply "Show" (en) / "Ver" (pt)
  │                                      └─ tap = inbound ─▶ window open ─▶ send full content free-form
  └─ window closed, template paused/rejected/recategorised ─▶ hold, outcome deferred, admin alert
```

Opt-in lives in `notification_preference` (one source of truth), which gains
a `proactive_opt_in` state with a per-platform default: enabled on Discord and
Slack (unchanged behaviour), not opted in on WhatsApp. `messaging_consent` is
not created. `ck_notification_outcome` gains `needs_template`,
`not_opted_in` and `deferred`.

| Purpose | Template (utility, approved per en_US and pt_BR) | Body variables |
|---|---|---|
| ALERT | `cf_alert_fired` | alert kind, short target label, id |
| SCHEDULED | `cf_scheduled_ready` | the task's short title |
| OBLIGATION digest | `cf_asks_digest` | count of new asks |
| LINK confirmation | `cf_link_confirm` | none |

Content held for a tap is stored in `pending_delivery` for 7 days, then
dropped. Obligation notifications on WhatsApp are batched into at most one
daily digest template. `WHATSAPP_DAILY_PROACTIVE_CAP` counts **unique
recipients of business-initiated templates over a rolling 24 hours** (Meta's
portfolio limit is expressed that way) and stops proactive templates before
the portfolio limit is reached; alerts have priority over digests. Each
template delivery records its category, cost and **currency** (Brazil moves
to BRL billing on 2026-07-01). Templates carry no URL unless it is a
verifiable HTTPS URL (a 2026 approval rule) and no promotional content, to
stay utility.

## Commands without slash commands

1. Adapter parses a leading `/word` or a localised keyword at the start of a
   message (`menu`, `help`/`ajuda`, `alerts`/`alertas`, `schedules`/
   `agendamentos`, `forget`/`esquecer`, `notifications`/`notificações`,
   `stop`/`parar`, `start`/`começar`, `link`/`vincular`), mapped through
   `CommandCatalog.keywords`.
2. Otherwise natural language through the existing routing (alert intent, fact
   replies, crypto routes).
3. `menu` is two levels, because the WhatsApp catalogue (schedule
   create/list/delete, alert list/delete, forget, notifications, resolve,
   link, unlink, stop, help) exceeds the 10-row list limit. Top level (≤10
   rows, 24-char titles): Wallets, Alerts, Schedules, Asks, Facts,
   Notifications, Link account, Forget, Stop, Help. Picking a group opens its
   own list (for example Alerts: list, delete); picking a leaf runs it.
4. Confirmations: reply buttons (≤3, 20-char labels) whose ids carry the
   choice-prompt id; a list for choosing one of several wallets (≤10). Outside
   the window, prompts cannot be sent at all; a confirmation always follows a
   person's message, so it is always inside it.

## Scope and disclosure (Meta 4.7)

- `WHATSAPP_TOOLS` default: `chain_balances`, `defi_positions`, `portfolio`,
  `wallet_activity`, `market` (crypto and FX only), `facts`, `alerts`,
  `schedules`, `time`; web search, Wikipedia, external-document links and
  general MCP servers off.
- Scope is decided by the deterministic routes (commands, alert intent, fact
  replies, crypto routes) and, when none matches, by **one classification
  call** that returns in-scope or out-of-scope and never produces answer
  text. An out-of-scope question gets a fixed, localised "on WhatsApp I help
  with your wallets, positions, alerts and reminders" reply; no
  reasoning-loop run and no answer-generating call happen.
- The allowlist and the scope check apply when a scheduled question is
  created on WhatsApp and again at every scheduled run; a run that is out of
  scope or needs a tool outside the allowlist is skipped and reported, not
  answered.
- The first reply to a new person and `help` state: what the assistant does,
  that Meta processes and may retain messages for up to 30 days, not to send
  seed phrases or private keys, and how to stop proactive messages.
- Seed phrases and private keys: the platform-neutral secret-material guard
  of `add-platform-ports` (enabled for WhatsApp by default) keeps them out of
  memory, traces and inbound rows and answers with a warning; a pasted
  transaction hash is not affected.

## Voice notes and images

Voice notes (`audio`, ogg/opus up to 16 MB) are downloaded immediately and
passed to the `SpeechToText` port of `add-platform-ports`. That change ships
only the port and a null implementation, so until a backend is chosen (task
0.7: AminiLLM on-prem or an external API, recorded as a data-flow decision
because audio is personal data) a voice note gets a fixed reply asking for
text. With a backend, the transcript is treated as the person's typed text,
the reply quotes the note, and the audio is deleted after transcription.
Images are out of scope for v1 (fixed reply); documents go through the
existing pipeline as owner-only personal documents (`document_entry` with
`owner_person_id`, `add-platform-ports`).

## Forgetting and deletion

User-side deletes produce no usable event, so nothing is deleted implicitly.
`forget` / `esquecer` erases conversation memory; "forget my phone" etc. uses
the existing fact flow.

`forget everything` (confirmed with a button) is an erasure under LGPD:

- For a person whose only identity is this WhatsApp one: insert
  `person_opt_out`, which fires the existing purge triggers (0008 corpus,
  0013 conversation memory, 0014 facts, including phone, email, address,
  birth date and wallets); then delete their alerts, schedules, notification
  rows, conversation summaries, owner-only documents, `trace_export` rows and
  Langfuse traces (through the Langfuse delete API, by the person's trace
  user id); then the WhatsApp identity, `whatsapp_recipient`,
  `service_window`, `pending_delivery` and `whatsapp_inbound` rows.
- For a linked person: remove only the WhatsApp identity, its link, its
  recipient, window, held deliveries and inbound rows, and the data created
  on WhatsApp; the shared person and their Discord/Slack data survive unless
  the person, in a second explicit confirmation, chooses to erase the whole
  person, which then runs the full erasure above.

Meta's own copy (up to 30 days) is outside our control and the disclosure
says so.

## Groups (verified status, 2026-09)

The Groups API needs an Official Business Account; groups are created by the
business only (invite link), at most 8 participants, text/media/templates
only, no interactive messages, no edits or deletes, no access to groups users
created. Specified as a later phase (`WHATSAPP_GROUPS=true`): a group becomes
a `ChannelRef` with ACL = current membership from group webhooks, indexed only
from when the business created it, confirmations fall back to 1:1. Off in this
change; every channel capability stays hidden.

## Feature parity

The single feature matrix is in `add-platform-ports/design.md` (Feature
matrix). WhatsApp-specific notes on it:

- No WhatsApp corpus: corpus answers only via a linked identity with
  `LINKED_CORPUS_ON_WHATSAPP=true`, private, plain-text citations.
- Asks are not extracted from 1:1 chats; ask notifications are a daily
  digest template for linked people, opt-in.
- Tables become lists; quick-reply label "Show" (en) / "Ver" (pt).
- Proactive messages: free-form in the window, else a utility template,
  charged, capped by unique recipients per rolling 24 h.
- Voice notes: via `SpeechToText` once a backend is chosen.
- External-document links: off (owner-only if an operator enables them).
- Admin console: number, templates and their status, costs with currency,
  masked ids.

## Open questions

1. Who signs off the narrowed persona under Meta 4.7? (Blocks go-live.)
2. Direct Cloud API under CyberdyneCorp's verified portfolio, or a BSP? New
   dedicated number or coexistence?
3. Users: internal team (linking matters) or external end users (pure 1:1)?
4. Budget for proactive templates; per-event alerts vs daily digest default?
5. Voice notes in the MVP, and which STT backend (AminiLLM or external)?
   Blocks only W6.3.
6. Groups phase (OBA) now or never?
7. Cloud API data-residency (local storage) region, and may linked corpus
   content reach WhatsApp at all (`LINKED_CORPUS_ON_WHATSAPP`)?
