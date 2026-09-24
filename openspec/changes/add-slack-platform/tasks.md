Depends on `add-platform-ports`. Every PR adds regression tests for what it
fixes or guards, updates `docs/slack-setup.md` / `docs/operations.md`, and
ships dark behind `ENABLED_PLATFORMS` not containing `slack`.

## 0. Before PR-S1

- [ ] 0.1 Record answers to the open questions in `design.md` (mode, plan, transport, backfill depth, Connect/guests, command naming)
- [ ] 0.2 Create the internal Slack app from `deploy/slack/manifest.yaml` in a test workspace
- [ ] 0.3 Verify on the test workspace whether retention purges emit `message_deleted`; adjust the reconcile task

## 1. PR-S1: Transport, install store, settings

- [ ] 1.1 Dependencies `slack_bolt`, `slack_sdk`; `adapters/slack/` skeleton and `SLACK_CAPABILITIES`
- [ ] 1.2 Settings `SLACK_MODE`, `SLACK_TRANSPORT`, tokens, signing secret, team/enterprise id; `federated` refuses to start
- [ ] 1.3 Migration: `slack_install` (encrypted tokens), `slack_inbound` (`event_key` PK, kind, encrypted payload, `received_at`, `processed_at`), `slack_user`, `slack_channel_membership`, `slack_reconcile_state`
- [ ] 1.4 `SlackIngress`: Socket Mode and HTTP (signature v0, 5-min window, `url_verification`); insert into `slack_inbound` before the ack, worker drains unprocessed rows with `SKIP LOCKED`, marks `processed_at`; 7-day cleanup
- [ ] 1.4a `entrypoints/slack.py` as the only process holding the Socket Mode connection, dispatching to ingest and controller paths
- [ ] 1.5 Foreign team/enterprise rejection; purge only on `app_uninstalled` or `tokens_revoked` naming the bot token (tokens now, data at the next retention run); user-token revocation deletes only that token
- [ ] 1.6 Liveness in `health.py`; secrets in redaction registries
- [ ] 1.7 Tests: forged/stale signature, retry dedup (also after restart), foreign install, uninstall purge, user token revoked purges nothing, ack under 3 s with a slow handler, a process restart after ack still answers the event, two processes never split the event stream

## 2. PR-S2: Capture and backfill

- [ ] 2.1 `SlackChatSource.stream`: message subtypes table in `design.md`, threads, own/bot messages skipped
- [ ] 2.2 Edits and deletes through the existing tombstone path; `channel_deleted` tombstones the channel, archived channels stay readable
- [ ] 2.3 `SlackChatSource.backfill` with `BackfillPage`, token bucket, `Retry-After`, `SLACK_BACKFILL_DAYS`, thread replies
- [ ] 2.4 Retention reconcile job with `SLACK_RECONCILE_DAYS`: history plus `conversations.replies` per thread, compare only a completely fetched window, abort without tombstoning on any error; age-based deletion through the existing `app/retention.py` `RetentionPolicy`
- [ ] 2.5 Files: `SlackAttachmentFetcher` (url_private), `file_deleted`/`file_unshared`
- [ ] 2.6 Tests: resume without gaps, 429 handling, `not_in_channel`, deleted message unretrievable, purge reconcile, a thread reply within the window is not tombstoned, a reconcile that fails mid-window tombstones nothing, channel deleted, mpim message without mention neither answered nor stored

## 3. PR-S3: ACL and audience

- [ ] 3.1 `SlackAclResolver` per the table in `design.md`; Grid per-workspace
- [ ] 3.2 Membership and user caches kept by events plus TTL refresh; fail closed
- [ ] 3.3 `SlackAudienceResolver`: im private, mpim GROUP_DIRECT, private channel = intersection, public channel = that workspace's indexed public channels only, public with guests/external and Connect = destination only, thread = parent
- [ ] 3.4 Regression tests at the repository layer: guest, single-channel guest, Connect external user, private non-member, deactivated user, mpim-not-DM, public channel with guest, a public channel whose three members all belong to private #exec does not use #exec evidence, Grid foreign workspace, Slack unreachable

## 4. PR-S4: Bot surface

- [ ] 4.1 `SlackMentionCodec` (`<@U…>`, `<#C…|name>`, `<!here>`), identifier patterns into the egress guard
- [ ] 4.2 `SlackRenderer` (`markdown_text`, mrkdwn, escaping, defang of `<!here>`/`<!channel>`/`<!everyone>`/`<!subteam^…>`, model-produced `<@U…>` as plain names, split), `SlackPermalinks`
- [ ] 4.3 Mentions and `im` into `ChatController`; replies in thread; progress via `chat.update` or assistant status
- [ ] 4.4 `/cyberfriend` umbrella parser from `CommandCatalog`; optional aliases; ephemeral replies via `response_url` with `im` fallback
- [ ] 4.5 Thread-bound actions by mention; "Ask CyberFriend about this" message shortcut
- [ ] 4.6 `/cyberfriend index|unindex` with join/invite flow, `SLACK_INDEX_MANAGERS`, `SLACK_ALLOW_CONNECT`
- [ ] 4.7 `SlackChoicePrompt`: Block Kit buttons, confirm object, requester and team check, `chat.update`
- [ ] 4.8 `SlackDirectMessenger` via `conversations.open`, per-channel 1 msg/s limiter, closed mapping
- [ ] 4.9 Reactions into the ask-acknowledgement sink; `users.info` locale hint
- [ ] 4.10 Tests: foreign button press, expired prompt, mpim fact withheld, facts-about-others with `<@U…>`, `<!channel>` defanged, model output with `<!subteam^S1>` does not notify, model-produced `<@U…>` rendered as a name, alert delivered to im

## 5. PR-S5: Admin and optional surfaces

- [ ] 5.1 Admin console Slack page: installs, scopes, per-channel ingest status (no tokens)
- [ ] 5.2 OAuth install flow for multi-workspace Grid (internal org only)
- [ ] 5.3 Optional App Home (`app_home_opened` + `views.publish`) and assistant pane (suggested prompts, status)
- [ ] 5.4 MCP tools accept Slack channel refs; tracing adds `platform`

## 6. PR-S6: End-to-end

- [ ] 6.1 `SlackWire`: Bolt app driven with signed event, command and `block_actions` payloads; fake Web API via `base_url`
- [ ] 6.2 Shared scenarios on Slack (corpus, privacy, alerts, schedules, facts, catch-up)
- [ ] 6.3 Slack-only scenarios: thread memory, Connect channel audience, guest, command in thread via mention
- [ ] 6.3a Multi-wire e2e scenarios (not only unit tests): a Slack question calls no Discord fake; `discord:123` and `slack:123` isolated; link code issued on Discord and redeemed on Slack
- [ ] 6.4 README feature map and `docs/slack-setup.md`

## 7. Later, optional: federated mode

- [ ] 7.1 `SlackNativeSearch` over `assistant.search.context` with per-user tokens and a 10/min per-user limiter
- [ ] 7.2 No-persistence guarantee test: no Slack content in the database, Langfuse traces, `trace_export` rows, memory turns (own turn and answer text only, no citations) or logs after an answer
- [ ] 7.3 Reduced features: catch-up via RTS, asks only when addressed, memory of own turns only, guests refused
- [ ] 7.4 HTTP transport only if Marketplace-listed
