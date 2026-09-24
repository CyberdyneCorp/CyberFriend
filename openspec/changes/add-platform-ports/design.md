## Principle

The core decides *what* to say, to *whom*, drawing on *which* evidence. A
platform decides *how* it is said and delivered. Everything that differs
between Discord, Slack and WhatsApp is either a capability flag the core reads
or a port an adapter implements. The core never compares a platform name.

## Identity

```
Platform         StrEnum: discord | slack | whatsapp
ExternalId       NewType(str)           opaque, never parsed by the core
PersonRef        (platform, user_id: ExternalId)
ChannelRef       (platform, channel_id: ExternalId)
MessageRef       (channel: ChannelRef, message_id: ExternalId)
ConversationRef  (platform, conversation_id: ExternalId, kind: ConversationKind,
                  workspace: ExternalId | None)
ConversationKind CHANNEL | PRIVATE_CHANNEL | THREAD | GROUP_DM | DM
```

- Discord: snowflakes as decimal strings (`"1234…"`). `workspace` = guild id.
- Slack: `U…/W…`, `C…/G…/D…`, message `ts`, `workspace` = `T…` (or `E…` on
  Enterprise Grid). A thread is `ConversationRef(kind=THREAD)` whose id is
  `"<channel>:<thread_ts>"`; the thread's `ChannelRef` is the parent channel,
  so ACL is always evaluated on the channel.
- WhatsApp: BSUID or E.164 digits for people, `wamid.…` for messages; the 1:1
  chat is `kind=DM`.

`ChannelRef` stops being reused for federated evidence
(`ChannelRef(source_system, 0)` today): evidence carries an `EvidenceOrigin`
(`CORPUS(channel)` or `EXTERNAL(source_system)`), so "corpus" no longer means
"discord".

Display fallbacks never use the id: `PersonRef.display_fallback()` returns a
neutral label, and `PersonRef.redacted()` is the only form logged. This
matters on WhatsApp, where the id is a phone number, and is enforced now so it
is not retrofitted later.

## Storage and migration

The migration keeps BIGINT surrogate keys instead of switching every key to
TEXT. Reasons: every foreign key, every `bigint[]` permission array
(`conversation_turn.channel_ids`, `summaries.covered_channel_ids`, document
`channel_ids`) and the 21 `CAST(:channel_ids AS bigint[])` sites keep working;
existing Discord rows need no rewrite because their surrogate stays equal to
their snowflake.

| Table / column | Change |
|---|---|
| `channel` | `platform` already exists (0001); add `external_id TEXT` (backfilled `id::text`), `UNIQUE(platform, external_id)`; new non-Discord rows take ids from a **descending negative sequence** (`START -1 INCREMENT -1`), disjoint from every snowflake by construction (snowflakes are positive) |
| `channel_workspace` (new) | `(channel_id, workspace_id TEXT)`, PK on both; Discord rows backfilled with the guild id; a Slack channel shared across Grid workspaces has one row per workspace; the Slack ACL reads it |
| `message` | add `external_id TEXT` (backfilled `id::text`), `UNIQUE(channel_id, external_id)`, `sequence TEXT` (adapter ordering key; Discord = zero-padded snowflake, Slack = `ts`, WhatsApp = receipt time); new ids from the same negative sequence |
| `message.reply_to_id`, `message.thread_id` | TEXT external refs (`USING col::text`) |
| `conversation_window.thread_id` (0001) | TEXT (`USING thread_id::text`), so a Slack `thread_ts` reaches windows |
| `ask.thread_id` (0002) | TEXT |
| `ask.source_message_id` (0002), `ask_reaction.message_id` (0002), `document_entry.message_id` (0003) | internal message ids (values unchanged for Discord, since internal id = snowflake); FK to `message.id` where missing |
| `person_platform_id.platform_user_id`, `mcp_token.platform_user_id` | TEXT |
| `conversation_turn.location_id` | TEXT |
| `ingest_cursor.oldest_message_id` | renamed `cursor`, TEXT |
| `message_tombstone`, `trace_export_message`, `notification_queue.source_message_id` | keyed by the internal message id |
| `document_entry.channel_id` | nullable; add `owner_person_id BIGINT NULL REFERENCES person`, `CHECK ((channel_id IS NULL) <> (owner_person_id IS NULL))`; `attachment_id` TEXT; the document chunk rows gain `owner_person_ids bigint[]` beside `channel_ids`, and the retrieval predicate becomes `channel_ids && :viewer_channels OR owner_person_ids && :viewer_persons` |
| `link_code` (new) | hashed six-digit code, issuing identity, redeeming identity, expiry, failed-attempt count, confirmed_at; used by identity linking |
| `position_alert`, `scheduled_task`, `notification_queue` | add `origin_platform TEXT` (backfilled `discord`) for delivery routing |
| `person_preference` (new) | `person_id`, `delivery_platform TEXT NULL` |
| runtime setting `indexed_channel_ids` | each entry rewritten `discord:<id>` |

Stores translate refs to internal ids in exactly one helper per store
(`channel_ids_for(refs) -> list[int]`, `message_id_for(ref)`), and rebuild
refs from the row's own `platform` and `external_id`. The downgrade restores
BIGINT columns and refuses to run while any non-Discord row exists.

## Capability matrix

```python
@dataclass(frozen=True)
class PlatformCapabilities:
    has_channels: bool
    has_threads: bool
    has_history_backfill: bool
    has_ephemeral: bool
    has_slash_menu: bool
    commands_in_threads: bool
    max_buttons: int            # 0 = no buttons
    has_list_picker: bool
    edits_and_deletes_are_events: bool
    has_reactions: bool
    max_plain_chars: int        # plain-text / fixed-reply limit
    max_rich_chars: int         # limit of the richest answer format
    markup: MarkupDialect       # DISCORD_MD | SLACK_MRKDWN | WHATSAPP
    proactive: ProactivePolicy  # FREE | WINDOW_OR_TEMPLATE
    has_permalinks: bool
```

| Flag | Discord | Slack | WhatsApp (1:1) |
|---|---|---|---|
| has_channels | yes | yes | no |
| has_threads | yes (guild threads) | yes (native `thread_ts`) | no (quote replies only) |
| has_history_backfill | yes | yes (internal app) | no |
| has_ephemeral | yes (interactions) | yes (`chat.postEphemeral`, response_url) | no |
| has_slash_menu | yes | yes (flat, one text arg) | no |
| commands_in_threads | yes | no | n/a |
| max_buttons | 25 | 25 per actions block | 3 |
| has_list_picker | yes (select) | yes (static select) | yes (10 rows) |
| edits_and_deletes_are_events | yes | yes (not retention purges) | no |
| has_reactions | yes | yes | yes |
| max_plain_chars | 2,000 | 4,000 (`text`) | 4,096 |
| max_rich_chars | 2,000 | 12,000 (`markdown_text`) | 4,096 |
| markup | Discord markdown | mrkdwn or standard Markdown block | `*b*` `_i_` `~s~` ``` ```m``` ``` |
| proactive | free | free | 24h window, else approved template |
| has_permalinks | yes | yes | no |

A golden test per platform asserts its declared `PlatformCapabilities`, so a
flag change is always a reviewed diff.

## Ports

All in `ports/platform.py` / `ports/inbound.py`; one implementation per
platform, found through the `PlatformRegistry`:

- `MessageRenderer.render(RichText, target) -> Sequence[Rendered]`
- `ReplySink.send(conversation, rendered, private_to: PersonRef | None)`;
  `private_to` on a platform without ephemeral replies is delivered through
  `DirectMessenger` instead, which the controller decides from capabilities.
- `ProgressIndicator.start/stop(conversation)` plus
  `refresh_interval: timedelta | None` (Discord edit/delete note, `None`;
  Slack assistant status or `chat.update`, `None`; WhatsApp typing indicator,
  which Meta drops after 25 s, so 20 s). The controller re-sends at the
  interval until the reply is sent.
- `DirectMessenger.send_direct(person, RichText, purpose) -> DeliveryResult`
  with results `SENT | CLOSED | FAILED | NEEDS_TEMPLATE`; `purpose` is
  `ANSWER | ALERT | SCHEDULED | OBLIGATION | NOTICE`. Replaces
  `DiscordTaskMessenger`, `DiscordNotificationSender` and the withheld-notice
  DM.
- `ChoicePrompt.offer(prompt, options, requester, window) -> ChoiceHandle`
  and inbound `ChoiceResponse(prompt_id, responder, option)`. Generalises
  `ConfirmationSurface` and alert confirmation. The responder always comes
  from the authenticated platform event, never from payload fields the client
  chose.
- `CommandSurface.register(catalog)` and inbound `CommandInvocation(name,
  args, asker, conversation, locale)`.
- `PermalinkBuilder(channel, message | None) -> str | None`.
- `MentionCodec`: `normalise(text) -> (text_with_sentinels, mentions,
  referenced_channels)`, `format_person`, `format_channel`,
  `identifier_patterns()`.
- `AttachmentFetcher.fetch(ref) -> bytes` (authenticated download).
- `SpeechToText.transcribe(audio, language_hint) -> Transcript | Unavailable`
  in `ports/speech.py`, with a `NullSpeechToText` default that always returns
  `Unavailable`. The real backend (AminiLLM on-prem or an external API) is a
  data-flow decision recorded before it is wired, because audio is personal
  data; it is not part of this change.
- Re-typed, kept: `AclResolver`, `AudienceResolver`, `AskerProfileResolver`,
  `ChannelAccessResolver` (takes a `ChannelRef`, returns the adapter's
  `missing_permission_label` instead of Discord permission names),
  `ChatSource` (`backfill(channel, cursor) -> BackfillPage`,
  `stream() -> AsyncIterator[InboundEvent]`).

`DeliveryMode` gains `GROUP_DIRECT` (Slack mpim, a future WhatsApp
business-created group; Discord bots cannot join group DMs, so Discord never
produces it): the audience is the intersection over its members and it is
**not** private, so DM-only facts stay withheld there. `EPHEMERAL` is chosen
only where `has_ephemeral`.

## Controller

`ChatController.handle_message(IncomingMessage) -> Sequence[Reply]` and
`handle_command(CommandInvocation)` carry what `CyberFriendClient.on_message`
and `_build_ask_command` do today: addressed-to-bot detection (decided by the
adapter, passed as `addressed_to_bot`), the empty-mention capability reply,
destination and conversation-location derivation, progress, rate-limit
wording, withheld notice, and splitting via the renderer. A `Reply` is
`RichText` + target (same conversation, same thread, private to asker) + an
optional `ChoicePrompt`.

## Rich text and splitting

`RichText` is a small immutable tree (`Paragraph`, `Bold`, `Italic`, `Code`,
`CodeBlock`, `BulletList`, `Subtext`, `Link`, `ChannelMention`,
`PersonMention`, `Table`). Model output stays constrained CommonMark and is
parsed into it; fixed replies are built directly. `adapters/chat_common/
splitting.py` holds the fence-aware splitter moved from
`adapters/discord/formatting.py`, parameterised by limit. The prompt's format
notice names the destination's dialect ("Discord markdown" today, byte for
byte the same text) through `MarkupDialect`.

## Command catalogue

```python
@dataclass(frozen=True)
class CatalogCommand:
    name: str                       # "alert"
    subcommands: tuple[str, ...]    # ("list", "delete")
    english: str; portuguese: str
    requires_channels: bool = False     # index, unindex, channels
    requires_conversation: frozenset[ConversationKind] | None = None
    keywords: Mapping[Language, tuple[str, ...]] = {}  # typed forms
```

Each adapter registers from it (Discord `app_commands` tree, Slack manifest
and umbrella parser, WhatsApp keyword parser). The existing test asserting the
Discord tree equals the list becomes "every adapter's registered surface
equals the catalogue filtered by that adapter's capabilities".
`_offered_where` filters by capabilities and conversation kind instead of
`guild_only`. `typed_command` on a platform without a slash menu *executes*
the command instead of telling the user to open a menu.

## Linking identities across platforms

Platform-neutral; lives in `app/identity_linking.py` and is used by any pair
of platforms (Discord<->Slack, Discord<->WhatsApp, Slack<->WhatsApp).

```
issuing platform DM:   "link"            ─▶ 6-digit code, 10 min, single use, hashed in link_code
redeeming platform:    "link 482913"     ─▶ pending; issuing DM gets ChoicePrompt
                                            "WhatsApp account ending …3f link request, Confirm?"
issuing platform:      Confirm           ─▶ person_platform_id(redeeming identity) → existing person
```

- No `person_link` table: the existing `person_platform_id(platform,
  platform_user_id) -> person_id` mapping is the link. Linking re-points the
  redeeming identity's row; data already held by the redeeming identity's old
  person (facts, wallets, alerts, schedules) is merged, conflicts keep both
  values and ask.
- Limits: a code is invalidated after 5 failed redemptions across all
  identities; 5 failures per redeeming identity per hour; a global hourly
  failure ceiling (`LINK_FAILURE_CEILING`, default 50) refuses all
  redemptions and alerts the operator.
- Unlink from either side re-points the removed identity at a new empty
  person; nothing is carried across.
- Viewer of a multi-linked person = union of each identity's live viewer,
  each resolved by its own platform's `AclResolver`. Audience rules for
  channel replies are unchanged.
- `app/limits.py` and opt-out (`app/optout.py`, `person_opt_out`) key on
  `person_id`, not `PersonRef`, so linking does not double the allowance.

## Delivery routing

Proactive items (alerts, scheduled answers, notifications) store
`origin_platform`. The sender delivers on
`person_preference.delivery_platform` when set and linked, else on
`origin_platform`. `CLOSED` on that platform closes the item there; it never
falls through to another platform silently.

## Secret-material guard

Platform-neutral, in `app/secret_guard.py`, enabled per platform by
`SECRET_GUARD_PLATFORMS` (default `whatsapp`; Discord snapshots unchanged
until an operator adds it). Detector: BIP-39 mnemonics of 12/15/18/21/24 words
with a valid checksum; a 64-hex string (optional `0x`) only when framed as a
key ("private key", "chave privada", "pk:", "secret key") and never when it
matches a transaction hash the wallet-activity path recognises. A match is
not written to memory, traces, trace export, inbound rows or logs, and gets a
warning reply.

## Feature matrix (single source)

This is the only feature matrix. `add-slack-platform` and
`add-whatsapp-platform` refer to it and add only platform-specific notes.

| Feature | Discord | Slack (internal) | WhatsApp (1:1) |
|---|---|---|---|
| Corpus answers with citations | yes | yes, permalinks (own index) | only via linked identity and `LINKED_CORPUS_ON_WHATSAPP=true`; plain-text citations |
| ACL-scoped retrieval | permissions_for | membership + public/guest/Connect rules | linked identities' union, else none |
| Audience-aware channel answers | yes | yes; Connect = that channel only; public channel = public channels only | n/a (always private) |
| Asks extraction | yes | yes | not a source |
| Ask notifications | DM | DM | daily digest template, linked people only, opt-in |
| /resolve | slash | `/cyberfriend resolve`, mention in thread | keyword, own asks |
| Conversation memory | yes | yes, thread = sub-place | yes, per 1:1 |
| Personal facts (DM-only) | yes | im only (mpim not private) | yes (every chat private) |
| Language EN/PT | yes | yes (+ users.info locale hint) | yes; templates per language |
| Temporal awareness | yes | yes | yes |
| Asker context / profile | display name, nickname, roles | display name, title | profile name only |
| Documents (channel) | yes | yes (`url_private`) | n/a |
| Documents (personal 1:1 upload) | owner-only | owner-only | owner-only |
| External-document links | channel-scoped | channel-scoped | off (owner-only when enabled) |
| Web search / Wikipedia | yes | yes | off by default (Meta 4.7) |
| MCP federation | yes | yes | allowlist (`WHATSAPP_TOOLS`) |
| Tool-approval prompts (agent-authorization) | View buttons | Block Kit + confirm object | reply buttons, in window |
| Market data | yes | yes | crypto/FX |
| Wallet balances, DeFi, portfolio | yes | yes | yes, lists not tables |
| Wallet activity (incl. address-poisoning flags) | yes | yes | yes; flags always shown |
| Alerts create/confirm | View buttons | Block Kit + confirm object | reply buttons, in window |
| Alert firing | DM | DM | DM in window, else utility template |
| Scheduled questions | DM | DM | template + "Show"/"Ver" |
| Catch-up / digest | yes | yes | no (hidden) |
| /channels /index /unindex | yes | yes (join / invite) | no (hidden) |
| /forget /notifications /schedule /alert | slash | `/cyberfriend <sub>` | keywords + menu |
| Answer disclosure / withheld notices | ephemeral or DM | ephemeral or im | n/a (always private) |
| Edits / deletes | events | events + retention reconcile | explicit forget only |
| Corpus opt-out and purge | yes | yes | yes (removes WhatsApp-held data) |
| Retention policy (`app/retention.py`) | yes | yes (same clock) | yes |
| Per-person rate limit | person_id | person_id | person_id |
| Secret-material guard | off by default | off by default | on |
| Voice | no | clips via `SpeechToText` (later) | via `SpeechToText` when a backend is chosen |
| Runtime configuration | yes | + Slack settings | + WhatsApp settings |
| Admin console | yes | + installs, ingest status | + number, templates, costs, masked ids |
| MCP interface | yes | yes | linked identity only |
| Tracing and trace export | yes | yes; federated mode redacts evidence | yes; ids masked, window and cost attributes |
| Proactive delivery routing | origin platform or preferred | same | same, under window/opt-in |
| e2e harness | DiscordWire | SlackWire | WhatsAppWire; multi-wire scenarios |

## PR sequencing

1. Domain types and `platform:id` parsing, adapters still Discord, no schema
   change: refs gain `ExternalId` with Discord adapters stringifying.
2. Migration + stores reading platform from rows + cursor/`BackfillPage`.
3. `PlatformCapabilities`, registry, routers, settings, per-platform secrets.
4. Outbound ports + `RichText` + renderer + shared splitter; Discord adapters
   implement them.
5. Inbound events, `MentionCodec`, `ChatController`, `CommandCatalog`,
   self-description by capability; Discord client thinned.
6. e2e harness generalised (including multi-wire scenarios); lint test
   forbidding `"discord"` in the core.
7. Identity linking, person-keyed limits and opt-outs, delivery routing,
   personal documents, the secret-material guard (flagged) and the
   `SpeechToText` port; lands before the first second platform ships.

The E.164 addition to the egress identifier guard ships as its own pull
request inside PR 5, with the Discord snapshot differences it causes listed
as an intended change.

Each PR passes the full Discord e2e suite with unchanged snapshots, except the
documented egress-guard pull request.

## Alternatives considered

- **TEXT primary keys everywhere.** Cleaner, but rewrites every foreign key
  and permission array, and every ACL query, in one migration on live data.
  Rejected for risk; the surrogate approach can move to TEXT later.
- **Branching on `platform == "slack"` in services.** Fast to write, and the
  exact coupling this change removes. Rejected.
- **Letting each adapter own a copy of the controller.** Rejected: the
  privacy rules live in that logic, and three copies drift.
