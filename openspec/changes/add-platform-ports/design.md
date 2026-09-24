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
| `channel` | add `platform` (backfilled `discord`), `external_id TEXT` (backfilled `id::text`), `UNIQUE(platform, external_id)`; new rows take ids from a sequence starting at 2^62, above any snowflake issued this century |
| `message` | add `external_id TEXT` (backfilled `id::text`), `UNIQUE(channel_id, external_id)`, `sequence TEXT` (adapter ordering key; Discord = zero-padded snowflake, Slack = `ts`, WhatsApp = receipt time) ; new ids from the same high sequence |
| `message.reply_to_id`, `thread_id` | TEXT external refs (`USING col::text`) |
| `person_platform_id.platform_user_id`, `mcp_token.platform_user_id` | TEXT |
| `conversation_turn.location_id` | TEXT |
| `ingest_cursor.oldest_message_id` | renamed `cursor`, TEXT |
| `message_tombstone`, `trace_export_message`, `notification_queue.source_message_id`, `document_entry.message_id/attachment_id` | keyed by the internal message id; attachment id TEXT |
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
    max_message_chars: int
    markup: MarkupDialect       # DISCORD_MD | SLACK_MRKDWN | WHATSAPP
    proactive: ProactivePolicy  # FREE | WINDOW_OR_TEMPLATE
    has_permalinks: bool
```

| Flag | Discord | Slack | WhatsApp (1:1) |
|---|---|---|---|
| has_channels | yes | yes | no |
| has_threads | yes | yes (native `thread_ts`) | no (quote replies only) |
| has_history_backfill | yes | yes (internal app) | no |
| has_ephemeral | yes (interactions) | yes (`chat.postEphemeral`, response_url) | no |
| has_slash_menu | yes | yes (flat, one text arg) | no |
| commands_in_threads | yes | no | n/a |
| max_buttons | 25 | 25 per actions block | 3 |
| has_list_picker | yes (select) | yes (static select) | yes (10 rows) |
| edits_and_deletes_are_events | yes | yes (not retention purges) | no |
| has_reactions | yes | yes | yes |
| max_message_chars | 2,000 | 4,000 text / 12,000 `markdown_text` | 4,096 |
| markup | Discord markdown | mrkdwn or standard Markdown block | `*b*` `_i_` `~s~` ``` ```m``` ``` |
| proactive | free | free | 24h window, else approved template |
| has_permalinks | yes | yes | no |

## Ports

All in `ports/platform.py` / `ports/inbound.py`; one implementation per
platform, found through the `PlatformRegistry`:

- `MessageRenderer.render(RichText, target) -> Sequence[Rendered]`
- `ReplySink.send(conversation, rendered, private_to: PersonRef | None)`;
  `private_to` on a platform without ephemeral replies is delivered through
  `DirectMessenger` instead, which the controller decides from capabilities.
- `ProgressIndicator.start/stop(conversation)` (Discord edit/delete note,
  Slack assistant status or `chat.update`, WhatsApp typing indicator).
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
- Re-typed, kept: `AclResolver`, `AudienceResolver`, `AskerProfileResolver`,
  `ChannelAccessResolver` (takes a `ChannelRef`, returns the adapter's
  `missing_permission_label` instead of Discord permission names),
  `ChatSource` (`backfill(channel, cursor) -> BackfillPage`,
  `stream() -> AsyncIterator[InboundEvent]`).

`DeliveryMode` gains `GROUP_DIRECT` (Slack mpim, Discord group DM, a future
WhatsApp group): the audience is the intersection over its members and it is
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

## Feature parity (target after the three changes)

| Feature | Discord | Slack | WhatsApp | Notes |
|---|---|---|---|---|
| Corpus answers with citations | yes | yes (internal app, own index) | no, except via linked identity | Slack federated mode would use Real-time Search |
| ACL-scoped retrieval | permissions_for | membership + public/guest rules | n/a (1:1) | SQL predicate everywhere |
| Audience-aware channel answers | yes | yes, Slack Connect = that channel only | n/a | GROUP_DIRECT not private |
| Asks extraction + DM notifications | yes | yes | delivery only, templated | |
| Conversation memory | yes | yes, thread as sub-place | yes (1:1) | |
| Personal facts (DM-only) | yes | yes, im only (mpim not private) | yes, every chat is 1:1 | |
| Language EN/PT | yes | yes (+ users.info locale hint) | yes; templates per language | |
| Documents | yes | yes (url_private) | personal 1:1 uploads | |
| Web + MCP federation | yes | yes | narrowed allowlist (Meta 4.7) | |
| Market, wallets, DeFi, portfolio, activity | yes | yes | yes | formatting only |
| Alerts with confirm buttons | View buttons | Block Kit + confirm object | reply buttons in window; utility template when firing | |
| Scheduled questions | yes | yes | template + "Show" button | |
| Catch-up / digest | yes | yes | no | hidden by capability |
| /channels /index /unindex | yes | yes (join/invite) | no | hidden by capability |
| /forget /resolve /notifications /schedule /alert | slash | `/cyberfriend <sub>` | keywords + list menu | |
| Edits / deletes | events | events + reconcile | explicit /forget only | |
| Voice notes | no | clip transcription (later) | transcription | shared STT port |
| Admin console, MCP, tracing | yes | + workspace/install | + number, template cost | platform:id everywhere |
| e2e harness | DiscordWire | SlackWire | WhatsAppWire | shared scenarios |

## PR sequencing

1. Domain types and `platform:id` parsing, adapters still Discord, no schema
   change: refs gain `ExternalId` with Discord adapters stringifying.
2. Migration + stores reading platform from rows + cursor/`BackfillPage`.
3. `PlatformCapabilities`, registry, routers, settings, per-platform secrets.
4. Outbound ports + `RichText` + renderer + shared splitter; Discord adapters
   implement them.
5. Inbound events, `MentionCodec`, `ChatController`, `CommandCatalog`,
   self-description by capability; Discord client thinned.
6. e2e harness generalised; lint test forbidding `"discord"` in the core.

Each PR passes the full Discord e2e suite with unchanged snapshots.

## Alternatives considered

- **TEXT primary keys everywhere.** Cleaner, but rewrites every foreign key
  and permission array, and every ACL query, in one migration on live data.
  Rejected for risk; the surrogate approach can move to TEXT later.
- **Branching on `platform == "slack"` in services.** Fast to write, and the
  exact coupling this change removes. Rejected.
- **Letting each adapter own a copy of the controller.** Rejected: the
  privacy rules live in that logic, and three copies drift.
