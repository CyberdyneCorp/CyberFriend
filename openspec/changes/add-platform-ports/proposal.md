## Why

CyberFriend is hexagonal in name but Discord-shaped in practice. A second
platform cannot be added as "just an adapter" today, because:

- **Ids are Discord snowflakes everywhere.** `PersonRef.platform_user_id`,
  `ChannelRef.platform_channel_id`, `Message.platform_message_id`,
  `ConversationLocation.platform_location_id`, `ScopeProvider.current()` and
  `ChatSource.backfill(before_message_id)` are `int`. The schema uses the
  snowflake itself as the BIGINT primary key of `channel` and `message`, with
  no platform in the key, and 21 SQL sites bind `CAST(:channel_ids AS
  bigint[])`. Slack ids are strings (`U0123`, `C0123`, message `ts`
  `1712345678.123456`), WhatsApp ids are phone digits or BSUIDs and `wamid.…`.
  Ingest computes its next cursor as `min(platform_message_id)`, which only
  works because snowflakes are time-ordered integers.
- **`PLATFORM = "discord"` is hard-coded in 17 modules**, including every
  Postgres store, so a row is rebuilt as a Discord ref whatever it is.
- **`"discord"` also means "corpus evidence"** (`CORPUS_SOURCE_SYSTEMS`,
  `Citation.source_system`), so a Slack citation would not be treated as
  channel-scoped evidence.
- **Replies are Discord markdown strings in the app layer** (`**bold**`,
  `-# subtext`, `<#id>`), and privacy guards recognise "someone else" only
  through Discord mention markup (`<@id>`), so a Slack `<@U0123>` would slip
  past the "facts about others" refusal and the egress identifier guard.
- **The conversation controller lives in the 1,640-line Discord client**
  (addressed-to-bot detection, progress note, withheld notice, splitting),
  and direct messages, confirmations, permalinks and the command catalogue are
  Discord implementations wired straight into neutral services.
- **The process cannot start without `DISCORD_TOKEN`.**

Slack (`add-slack-platform`) and WhatsApp (`add-whatsapp-platform`) both need
the same refactor. Doing it once, first, with Discord behaviour unchanged, is
cheaper and safer than doing it twice inside two feature changes.

## What Changes

A behaviour-preserving refactor, delivered as seven pull requests (see
`tasks.md`), after which Discord is one registered platform among possibly
several:

- **Opaque string ids and a `Platform` enum** in the domain; `PersonRef`,
  `ChannelRef`, a new `MessageRef` and a new `ConversationRef` with a
  `ConversationKind` (channel, private channel, thread, group DM, DM).
- **Migration** (next free number, 0023 at time of writing) that keeps BIGINT
  surrogate keys (existing Discord rows keep `id = snowflake`, so no foreign
  key or `bigint[]` permission array is rewritten) and allocates non-Discord
  ids from a descending negative sequence, adds `external_id TEXT` with
  `UNIQUE(platform, external_id)` to `channel` (whose `platform` column
  already exists) and `UNIQUE(channel_id, external_id)` to `message`, adds a
  `channel_workspace` table for Slack Grid, turns user, location, cursor and
  every thread column (`message`, `conversation_window`, `ask`) into TEXT,
  points ask, ask-reaction and document message references at internal
  message ids, lets a document entry be owned by a person instead of a
  channel, and rewrites the indexed-channel runtime setting to `platform:id`
  entries.
- **Ordering and cursors stop depending on id order**: `ChatSource.backfill`
  returns a `BackfillPage(messages, next_cursor, exhausted)` with an opaque
  cursor chosen by the adapter.
- **A `PlatformCapabilities` value object** per platform (channels, threads,
  history backfill, ephemeral replies, slash menu, buttons and their limit,
  list pickers, edits/deletes as events, reactions, plain and rich message
  length, markup dialect, proactive-message policy, permalinks). App services
  branch on capabilities, never on platform names.
- **Platform ports** in `ports/platform.py` and `ports/inbound.py`:
  `MessageRenderer`, `ReplySink`, `ProgressIndicator`, `DirectMessenger`,
  `ChoicePrompt` (generalising `ConfirmationSurface` and alert confirmation),
  `CommandSurface`, `PermalinkBuilder`, `MentionCodec`, `AttachmentFetcher`,
  `SpeechToText` (with a null implementation), and the neutral inbound events `IncomingMessage`, `CommandInvocation`,
  `ChoiceResponse`, `ReactionEvent`. `AclResolver`, `AudienceResolver`,
  `AskerProfileResolver` and `ChannelAccessResolver` are kept and re-typed.
- **A `PlatformRegistry`** built in the composition root from
  `ENABLED_PLATFORMS`; services receive routers keyed by `ref.platform` that
  fail closed for a platform that is not registered.
- **Neutral rich text.** The app layer emits a small `RichText` model
  (paragraphs, bold, italic, code, lists, subtext, links, channel and person
  mentions); each platform's renderer converts, escapes, defangs mass and
  user-group mentions, renders model-produced person mentions as plain names,
  and splits at its own limit with one shared fence-aware splitter.
- **A `CommandCatalog`** replaces the `Command` list in `self_description.py`
  as the single source of commands, each declaring what it requires
  (channels, a slash menu, buttons). Self-description and command hints list
  only what the asker's platform and conversation kind can offer.
- **A `ChatController`** in `app/` takes neutral inbound events and returns
  replies; the Discord client shrinks to translation and delivery.
- **Mention sentinels.** Inbound adapters replace platform mention markup with
  neutral sentinels before routing, so fact-privacy and routing rules hold on
  every platform; the egress identifier guard aggregates every enabled
  platform's identifier patterns plus an E.164 phone pattern.
- **Platform-qualified identifiers at the edges**: credential holders, MCP
  tokens, MCP tool parameters and admin-console account ids accept
  `platform:id`; a bare numeric id keeps meaning Discord.
- **Per-platform settings**: `ENABLED_PLATFORMS` (default `discord`) and
  nested optional settings; only an enabled platform's secrets are required.
- **The e2e harness is generalised** to a platform-neutral scenario API over
  pluggable wires, with the existing Discord wire unchanged in behaviour, and
  scenarios that drive several wires against one process.
- **Cross-platform identity linking** for any pair of platforms, by a
  one-time code confirmed on the issuing platform, recorded by pointing a
  `person_platform_id` row at the existing person; rate limits and opt-outs
  keyed on the person; the viewer of a linked person is the union of each
  identity's live viewer.
- **Delivery routing**: proactive items are delivered on the platform they
  were created on unless the person sets a preferred delivery platform.
- **Personal documents**: a document uploaded in a 1:1 conversation is
  visible only to its uploader.
- **A secret-material guard** (seed phrases, framed private keys), platform
  neutral, enabled per platform and off for Discord by default.
- **One feature matrix** covering every feature on every platform, which the
  Slack and WhatsApp changes reference instead of copying.

Non-goals:

- Any Slack or WhatsApp adapter (their own changes).
- Any user-visible change on Discord other than the E.164 egress check, which
  ships in its own pull request as a documented, intended snapshot change;
  every other existing e2e snapshot SHALL pass unchanged.
- A real speech-to-text backend (only the port and a null implementation).

## Capabilities

### New Capabilities

- `platform-identity`: how people, channels, messages and conversations are
  identified across platforms, how they are stored, how the existing Discord
  data migrates, and how identities on several platforms are linked to one
  person.
- `platform-surface`: the capability matrix, the platform ports and their
  routing, neutral rich text, the command catalogue, self-description by
  capability, mention neutralisation, per-platform configuration, progress
  refresh, delivery routing, the secret-material guard and the speech port.
- `multi-platform-testing`: the platform-neutral e2e scenario API and the
  guarantees it gives every platform.

### Modified Capabilities

Existing requirements worded in Discord terms are generalised to "the source
platform", with Discord behaviour unchanged:

- `channel-acl`: visibility is resolved by the source platform's access rules.
- `chat-indexing`: scope changes need the platform's channel-management
  authority.
- `rich-formatting`: the platform renderer's markup and limit; mention
  suppression covers user groups and model-produced person mentions.
- `document-ingestion`: visibility of the source channel; new requirement for
  owner-only 1:1 uploads.
- `external-document-access`: visibility comes from the source platform.
- `message-retrieval`: citations open the message on its platform.
- `asker-context`: the profile fields each platform supplies.
- `self-description`: commands listed per platform and conversation kind.
- `external-sources`: new requirement that outbound queries carry no platform
  identifier or phone number (the E.164 part is new on Discord, see Non-goals).

`discord-bot-surface`, `answer-disclosure`, `message-ingestion` and `testing`
keep every requirement; this change moves where they are implemented.

## Impact

- Domain: `domain/identity.py`, `domain/messages.py`, `domain/audience.py`,
  new `domain/platform.py`.
- Ports: `ports/sources.py`, `ports/store.py`, `ports/memory.py`,
  `ports/acl.py`, `ports/answers.py`, `ports/notifications.py`, new
  `ports/platform.py`, `ports/inbound.py`, `ports/speech.py`.
- App: `ingest.py`, `scope.py`, `indexing.py`, `channel_listing.py`,
  `catchup.py`, `routing.py`, `self_description.py`, `confirmation.py`,
  `alerts.py`, `alert_requests.py`, `schedules.py`, `reasoning/*`,
  `asks/*`, `windowing.py`, `limits.py`, `optout.py`, `documents/*`, new
  `chat_controller.py`, `rich_text.py`, `commands.py`, `identity_linking.py`,
  `delivery_routing.py`, `secret_guard.py`.
- Adapters: every store (`adapters/store/*`, `adapters/documents/store.py`),
  `adapters/web/query.py`, `adapters/discord/*`, new
  `adapters/chat_common/splitting.py`.
- Composition, entrypoints, `config.py`, `app/configuration.py`, `mcp/*`,
  `admin/*`, `tests/e2e/harness/*`.
- One migration, reversible for Discord-only data.
- New settings: `ENABLED_PLATFORMS`, `SECRET_GUARD_PLATFORMS`,
  `LINK_FAILURE_CEILING`.

## Risk

This touches the data model, the ACL path and authentication identifiers, so
it is the riskiest change of the three despite adding no feature. The
mitigations: the migration keeps every existing primary key and permission
array value; the ACL stays a SQL predicate bound to internal channel ids; the
routers fail closed on an unknown platform; the Discord e2e suite, snapshot
for snapshot, is the acceptance gate of every PR; and a test forbids the
literal `"discord"` outside `adapters/discord`, entrypoints and composition so
the coupling cannot quietly return.
