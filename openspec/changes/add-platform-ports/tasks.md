Every PR below SHALL pass the full Discord e2e suite with unchanged snapshots
(except the separate egress-guard pull request in 5.3, whose snapshot
differences are listed as intended),
update `docs/` where behaviour or configuration is described, and keep
per-function cognitive complexity within the backend target (15).

## 1. PR-P1: Domain identity (no schema change)

- [ ] 1.1 `domain/platform.py`: `Platform`, `ExternalId`, `ConversationKind`, `ConversationRef`, `MessageRef`
- [ ] 1.2 `PersonRef`/`ChannelRef` ids become `ExternalId`; Discord adapters and stores stringify at the boundary
- [ ] 1.3 `EvidenceOrigin` replaces `ChannelRef(source_system, 0)`; `CORPUS_SOURCE_SYSTEMS` derived from registered chat platforms
- [ ] 1.4 `PersonRef.display_fallback()` and `redacted()`; windowing, asks prompt, person creation and logs use them
- [ ] 1.5 `platform:id` parser shared by credential holders, MCP tokens/tools and admin ids; bare digits mean Discord
- [ ] 1.6 Tests: same id on two platforms, display fallback never shows id, parser accepts/rejects

## 2. PR-P2: Migration and stores

- [ ] 2.1 Migration (next free number): `external_id`/`sequence` columns (`channel.platform` already exists), unique constraints, descending negative id sequence, `channel_workspace`, TEXT user/location/cursor/reply columns and every thread column (`message.thread_id`, `conversation_window.thread_id`, `ask.thread_id`), `ask.source_message_id`/`ask_reaction.message_id`/`document_entry.message_id` as internal message ids, nullable `document_entry.channel_id` with `owner_person_id` and the exactly-one CHECK, `origin_platform` on alerts/schedules/notifications, `person_preference`, `link_code`
- [ ] 2.2 Data step rewriting `indexed_channel_ids` to `discord:<id>`
- [ ] 2.3 Downgrade that refuses while non-Discord rows exist
- [ ] 2.4 Stores read `platform`/`external_id` from rows; one `channel_ids_for`/`message_id_for` helper per store; remove store `PLATFORM` constants
- [ ] 2.5 `BackfillPage` and opaque cursor in `ChatSource`; `app/ingest.py` and the Discord reconciler stop using `min(id)`
- [ ] 2.6 `ScopeProvider.current() -> frozenset[ChannelRef]`
- [ ] 2.7 Integration tests: migration on a seeded Discord database (ids, ACL results, scope, asks, ask reactions, windows and document entries unchanged), a Slack-shaped thread id stored in windows and asks, resume without gaps, downgrade refusal

## 3. PR-P3: Capabilities, registry, settings

- [ ] 3.1 `PlatformCapabilities` (with `max_plain_chars`/`max_rich_chars`) and `DISCORD_CAPABILITIES`; golden test asserting each platform's declared capabilities
- [ ] 3.2 `PlatformRegistry` and fail-closed routers for ACL, audience, profile, channel access
- [ ] 3.3 `ENABLED_PLATFORMS` and nested optional Discord settings; per-platform required secrets; secrets registry, audit redaction, `FORBIDDEN_CREDENTIALS` enumerate platforms
- [ ] 3.4 `DeliveryMode.GROUP_DIRECT`; `EPHEMERAL` only where supported
- [ ] 3.5 Tests: default config unchanged, unknown platform yields empty viewer, group DM withholds DM-only facts

## 4. PR-P4: Outbound ports and rendering

- [ ] 4.1 `RichText` model and CommonMark-subset parser
- [ ] 4.2 `adapters/chat_common/splitting.py` (fence-aware, parameterised)
- [ ] 4.3 `MessageRenderer`, `ReplySink`, `ProgressIndicator` (with `refresh_interval`), `DirectMessenger`, `PermalinkBuilder`, `AttachmentFetcher` ports; Discord implementations; renderer defangs mass and user-group mentions and renders model-produced person mentions as plain names
- [ ] 4.4 `ChoicePrompt` port; `ConfirmationSurface` and alert confirmation go through it; requester check kept
- [ ] 4.5 Alerts, schedules, notifications and withheld notices use `DirectMessenger`
- [ ] 4.6 Fixed replies in `alerts.py`, `alert_requests.py`, `catchup.py`, `self_description.py`, renderers emit `RichText`
- [ ] 4.7 Format notice in `reasoning/stages.py` from `MarkupDialect` (Discord text byte-identical)
- [ ] 4.8 Tests: renderer golden files equal today's Discord output; foreign responder refused

## 5. PR-P5: Inbound events, controller, catalogue

- [ ] 5.1 `IncomingMessage`, `CommandInvocation`, `ChoiceResponse`, `ReactionEvent`
- [ ] 5.2 `MentionCodec` with sentinels; routing, catch-up and fact-privacy regexes match sentinels
- [ ] 5.3 Egress identifier guard aggregates codec patterns plus E.164, in its own pull request with a Discord regression scenario and the intended snapshot differences listed
- [ ] 5.4 `ChatController` extracted from `adapters/discord/bot.py`
- [ ] 5.5 `CommandCatalog` with `requires`; `_offered_where` by capability; surface-equals-catalogue test per platform
- [ ] 5.6 `ChannelAccessResolver` takes `ChannelRef` and returns `missing_permission_label`
- [ ] 5.7 Tests: facts-about-others refusal with sentinel input, egress guard with Slack id and phone

## 6. PR-P6: Harness and guard rails

- [ ] 6.1 Platform-neutral scenario API; `DiscordWire` wraps the existing fake
- [ ] 6.2 Scenario capability declarations and visible skips
- [ ] 6.3 Privacy scenarios parametrised over wires
- [ ] 6.3a Multi-wire scenarios against one assembled process (same id on two platforms isolated, no Discord fake called for a Slack asker)
- [ ] 6.4 Test forbidding platform literals in `domain/`, `ports/`, `app/`
- [ ] 6.5 Docs: `docs/operations.md` (platforms, settings), README architecture section and the feature matrix from `design.md`

## 7. PR-P7: Linking, routing and cross-platform guards

- [ ] 7.1 `app/identity_linking.py`: code issue in private conversations only, hashed `link_code`, 10-min expiry, per-code invalidation after 5 failures, per-identity 5/h, global `LINK_FAILURE_CEILING` with operator alert, confirmation `ChoicePrompt` on the issuing platform
- [ ] 7.2 Link recorded by re-pointing `person_platform_id`; merge of facts, wallets, alerts, schedules with conflict prompts; unlink re-points to a new empty person
- [ ] 7.3 Linked viewer = union of each identity's live viewer through its own resolver
- [ ] 7.4 `app/limits.py` and opt-out keyed on `person_id`
- [ ] 7.5 Delivery routing: `origin_platform`, `person_preference.delivery_platform`, `CLOSED` does not fall through
- [ ] 7.6 Owner-only 1:1 documents: store, retrieval predicate, forget/opt-out deletion
- [ ] 7.7 `app/secret_guard.py` (BIP-39 checksum, framed private keys, tx-hash exemption) behind `SECRET_GUARD_PLATFORMS`
- [ ] 7.8 `ports/speech.py` with `NullSpeechToText`
- [ ] 7.9 Tests: code in a channel refused, one code guessed from several identities invalidated, global ceiling alerts, no link without issuing-platform confirmation, unlink, rate limit shared across linked identities, alert routed to origin then preferred platform, another person never retrieves a 1:1 document, pasted tx hash not refused, seed phrase not stored
