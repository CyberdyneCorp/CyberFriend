## Principle

On WhatsApp, CyberFriend is a **1:1 personal assistant for a person's own
crypto positions, alerts, schedules and facts**. It is not a window onto team
channels, except through an identity the person has proven is theirs.

## Platform setup

Cloud API only (direct, or via a BSP such as Twilio or 360dialog that resells
it; the adapter talks Graph API shapes and a BSP is a base-URL and auth
change). Requires a Meta app, a Business portfolio, a WABA, a registered
number, a system-user permanent token and a webhook subscription. Business
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
    has_reactions=True, max_message_chars=4096, markup=MarkupDialect.WHATSAPP,
    proactive=ProactivePolicy.WINDOW_OR_TEMPLATE, has_permalinks=False,
)
```

Every chat is `ConversationKind.DM`; `AudienceResolver.resolve_private` is the
only audience; `AclResolver` returns an empty visible set unless the person is
linked (below).

## Inbound

```
Meta ──POST /whatsapp/webhook──▶ verify X-Hub-Signature-256 (HMAC-SHA256 of raw body,
                                  app secret, constant-time) ──▶ 200 within ~1 s
                                  └─▶ INSERT whatsapp_inbound(wamid) ON CONFLICT DO NOTHING
                                        └─▶ worker: parse ▶ window touch ▶ controller
```

- `messages[].type`: `text`, `interactive` (`button_reply.id`,
  `list_reply.id`), `button` (template quick reply), `reaction`, `audio`,
  `document`, `image`, `video`, `location`, `contacts`, `unsupported`.
- `statuses[]`: `sent/delivered/read/failed` update the delivery record and
  template cost; `failed` with a re-engagement error marks the window closed.
- Out-of-order delivery is expected; replies quote the triggering message via
  `context.message_id`.
- Read receipt and typing indicator are sent together as the progress
  indicator (clears on reply or after 25 s).

## Identity

- Key: `PersonRef("whatsapp", <BSUID>)` when present; for a legacy payload
  without BSUID, `PersonRef("whatsapp", "p:" + sha256(pepper || E.164))`, so
  no raw phone number becomes an id. When both are later seen for one user
  they are merged.
- The phone number (if the payload carries it) is not saved silently; the
  assistant may offer to save it as the person's phone fact, masked in every
  display except to its owner in the 1:1 chat.
- `contacts[].profile.name` is the display name; never the id.

## Linking to an existing person

```
Discord/Slack DM:  "link whatsapp"  ─▶ code 6 digits, 10 min, single use, bound to that person
WhatsApp 1:1:      "link 123456"    ─▶ person_link(whatsapp ref → person_id)
                                       confirmation shown on BOTH platforms
```

The code is issued only in a private conversation, stored hashed, rate-limited
(5 attempts per WhatsApp identity per hour, then locked 1h), and the link can
be removed from either side (`unlink`). Linking merges facts, wallets, alerts
and schedules under one person; conflicts keep both values and ask. A linked
WhatsApp person's viewer is the linked identity's viewer on the source
platform, recomputed on each question; answers stay private (1:1), so no
audience widening is possible, and citations render as plain text names (no
Discord deep links unless the operator enables them).

## Delivery policy

```
send_direct(person, text, purpose)
  ├─ no opt-in for proactive purposes ─▶ NOT SENT (recorded)
  ├─ window open (last inbound < 24h, margin 15 min) ─▶ free-form text/interactive
  └─ window closed ─▶ template[purpose][language] with quick reply "Show"/"Ver"
                         └─ tap = inbound ─▶ window open ─▶ send full content free-form
```

| Purpose | Template (utility, approved per en_US and pt_BR) | Body variables |
|---|---|---|
| ALERT | `cf_alert_fired` | alert kind, short target label, id |
| SCHEDULED | `cf_scheduled_ready` | the task's short title |
| OBLIGATION digest | `cf_asks_digest` | count of new asks |
| LINK confirmation | `cf_link_confirm` | none |

Content held for a tap is stored in `pending_delivery` for 7 days, then
dropped. Obligation notifications on WhatsApp are batched into at most one
daily digest template. A per-deployment `WHATSAPP_DAILY_PROACTIVE_CAP` stops
proactive templates before the portfolio limit is reached; alerts have
priority over digests. Templates carry no URL unless it is a verifiable HTTPS
URL (a 2026 approval rule) and no promotional content, to stay utility.

## Commands without slash commands

1. Adapter parses a leading `/word` or a localised keyword at the start of a
   message (`menu`, `help`/`ajuda`, `alerts`/`alertas`, `schedules`/
   `agendamentos`, `forget`/`esquecer`, `notifications`/`notificações`,
   `stop`/`parar`, `start`/`começar`, `link`/`vincular`), mapped through
   `CommandCatalog.keywords`.
2. Otherwise natural language through the existing routing (alert intent, fact
   replies, crypto routes).
3. `menu` returns a list message (≤10 rows, 24-char titles) of the catalogue
   entries WhatsApp supports; picking a row runs it.
4. Confirmations: reply buttons (≤3, 20-char labels) whose ids carry the
   choice-prompt id; a list for choosing one of several wallets (≤10). Outside
   the window, prompts cannot be sent at all; a confirmation always follows a
   person's message, so it is always inside it.

## Scope and disclosure (Meta 4.7)

- `WHATSAPP_TOOLS` default: `chain_balances`, `defi_positions`, `portfolio`,
  `wallet_activity`, `market` (crypto and FX only), `facts`, `alerts`,
  `schedules`, `time`; web search, Wikipedia and general MCP servers off.
- Questions outside scope get a fixed, localised "on WhatsApp I help with your
  wallets, positions, alerts and reminders" reply, not a model answer.
- The first reply to a new person and `help` state: what the assistant does,
  that Meta processes and may retain messages for up to 30 days, not to send
  seed phrases or private keys, and how to stop proactive messages.
- Messages that look like a seed phrase or private key are not stored in
  memory or traces and get a warning reply.

## Voice notes and images

Voice notes (`audio`, ogg/opus up to 16 MB) are downloaded immediately and
passed to the `SpeechToText` port owned by the voice-transcription change
(feature 10). The transcript is treated as the person's typed text, the reply
quotes the note, and the audio is deleted after transcription. Without that
port, a fixed reply asks for text. Images are out of scope for v1 (fixed
reply); documents go through the existing pipeline, scoped to the person only.

## Forgetting and deletion

User-side deletes produce no usable event, so nothing is deleted implicitly.
`forget` / `esquecer` erases conversation memory; "forget my phone" etc. uses
the existing fact flow; `forget everything` removes the WhatsApp identity,
its window, consent, pending deliveries and inbound dedup rows. Meta's own
copy (up to 30 days) is outside our control and the disclosure says so.

## Groups (verified status, 2026-09)

The Groups API needs an Official Business Account; groups are created by the
business only (invite link), at most 8 participants, text/media/templates
only, no interactive messages, no edits or deletes, no access to groups users
created. Specified as a later phase (`WHATSAPP_GROUPS=true`): a group becomes
a `ChannelRef` with ACL = current membership from group webhooks, indexed only
from when the business created it, confirmations fall back to 1:1. Off in this
change; every channel capability stays hidden.

## Feature parity

| Feature | Discord | Slack | WhatsApp | Notes |
|---|---|---|---|---|
| Corpus answers + citations | yes | yes | only via linked identity, private, plain-text citations | no WhatsApp corpus |
| ACL | permissions_for | membership rules | linked identity's viewer, else none | fail closed |
| Audience-aware channel answers | yes | yes | n/a (always private) | |
| Catch-up / digest | yes | yes | no (hidden) | groups phase could add |
| /channels /index /unindex | yes | yes | no (hidden) | |
| Asks extraction | yes | yes | not a source | 1:1 is not a team channel |
| Ask notifications | DM | DM | daily digest template | opt-in |
| /resolve | yes | yes | keyword, own asks | |
| Conversation memory | yes | yes | yes, per 1:1 | |
| Personal facts | DM-only | im-only | yes (every chat private) | phone offered, never silent |
| Language EN/PT | yes | yes | yes; templates per language | |
| Documents | channel-scoped | channel-scoped | personal only | |
| Web search / Wikipedia | yes | yes | off by default | Meta 4.7 |
| MCP federation | yes | yes | allowlist | |
| Market data | yes | yes | crypto/FX | |
| Wallet balances, DeFi, portfolio, activity | yes | yes | yes | lists, no tables |
| Alerts create/confirm | buttons | Block Kit | reply buttons | in window |
| Alert firing | DM | DM | DM in window, else utility template | charged |
| Scheduled questions | DM | DM | template + "Show" | charged outside window |
| /forget | yes | yes | keyword | explicit only |
| /notifications | yes | yes | keyword + opt-in/out | |
| Edits/deletes | events | events | none | |
| Voice notes | no | later | yes via STT port | feature 10 |
| Admin console | yes | yes | number, templates, costs, masked ids | |
| MCP interface | yes | yes | linked identity only | |
| Tracing | yes | yes | + window, template cost; ids masked | |
| e2e | DiscordWire | SlackWire | WhatsAppWire | |

## Open questions

1. Who signs off the narrowed persona under Meta 4.7? (Blocks go-live.)
2. Direct Cloud API under CyberdyneCorp's verified portfolio, or a BSP? New
   dedicated number or coexistence?
3. Users: internal team (linking matters) or external end users (pure 1:1)?
4. Budget for proactive templates; per-event alerts vs daily digest default?
5. Voice notes in the MVP (requires feature 10), and which STT backend?
6. Groups phase (OBA) now or never?
