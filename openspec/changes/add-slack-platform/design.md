## Decision: internal app first, federated mode designed but off

| | Internal (built here) | Federated (later, optional) |
|---|---|---|
| Who installs | CyberdyneCorp only (one workspace or one Grid org) | other organisations |
| Our index | allowed, kept | forbidden: nothing persisted beyond a short answer-time cache |
| History API | Tier 3, 1,000 per page | 1 req/min, 15 per page (non-Marketplace) |
| Retrieval | pgvector + BM25 as on Discord | `assistant.search.context` with the viewer's user token |
| Transport | Socket Mode (default) or HTTP | HTTP only if Marketplace-listed |
| Catch-up, asks, memory from others' messages | yes | reduced or off |

`SLACK_MODE` selects adapters in the composition root. The core is the same.
In internal mode the install store accepts only the configured
`SLACK_TEAM_ID` or `SLACK_ENTERPRISE_ID`; an install event from any other
organisation is rejected and logged. That is the technical guard behind the
policy line.

## Transports

- **Socket Mode** (default): app-level token `xapp-` with
  `connections:write`; one connection per process (limit is 10); every
  envelope is acknowledged immediately, before any work; `refresh_requested`
  and disconnect warnings trigger reconnect. Liveness = a connection
  acknowledged within the last N minutes.
- **HTTP**: `/slack/events`, `/slack/interactivity`, `/slack/commands`,
  `/slack/oauth/*` on the existing web server; `X-Slack-Signature` v0 HMAC
  over `v0:{timestamp}:{body}` with the signing secret, timestamps older than
  5 minutes rejected, constant-time compare; `url_verification` answered.
  Retries (`X-Slack-Retry-Num`) are deduplicated by `event_id`.

Both transports feed one `SlackIngress` that acks first and hands the payload
to a worker queue. Ack budget: 3,000 ms for everything.

## Capture

Events subscribed (bot): `message.channels`, `message.groups`, `message.im`,
`message.mpim`, `app_mention`, `reaction_added`, `reaction_removed`,
`member_joined_channel`, `member_left_channel`, `channel_created`,
`channel_rename`, `channel_archive`, `channel_unarchive`, `channel_deleted`,
`channel_shared`, `channel_unshared`, `group_*` equivalents, `user_change`,
`team_join`, `file_shared`, `file_deleted`, `file_change`, `file_unshared`,
`app_home_opened`, `tokens_revoked`, `app_uninstalled`.

Message subtypes:

| Subtype | Handling |
|---|---|
| none / `thread_broadcast` / `file_share` | store; `thread_ts` → thread; files → document pipeline |
| `message_changed` | update content, `edited_at`; rebuild affected windows |
| `message_deleted` | tombstone, rebuild windows, withdraw dependent asks |
| `message_replied` | refresh thread metadata only |
| `bot_message`, our own messages | not indexed (as on Discord) |
| `channel_join` etc. | membership cache only |

Identity of a message: `MessageRef(ChannelRef("slack", C…), ts)`; `sequence`
= `ts`, which sorts lexicographically after zero-padding the integer part.

Only channels in the indexing scope are stored; DMs and mpims with the bot
are handled as conversations (memory, facts) and are never indexed into the
corpus, matching Discord.

## Backfill

When a channel enters the scope (bot joined and `/cyberfriend index`), a
backfill job pages `conversations.history` newest-first with `latest` = the
stored cursor and `limit=200` (well under 1,000 to keep pages cheap), then
`conversations.replies` for each thread root seen. The pager honours
`Retry-After` on 429, keeps a per-method token bucket below Tier 3, stops at
`SLACK_BACKFILL_DAYS` (default 90) and stores `next_cursor` as the opaque
`BackfillPage` cursor. `SourceUnavailable` on `not_in_channel`,
`channel_not_found`, `missing_scope`. Resume is exact because the cursor is
the oldest `ts` stored plus the Slack pagination cursor.

## Retention reconcile

Slack retention purges are believed not to emit `message_deleted` (to verify
against a test workspace; tracked as an open task). A reconcile job re-reads
`conversations.history` for the last `SLACK_RECONCILE_DAYS` (default 7) per
indexed channel daily and, when the workspace has a retention policy
configured by the operator (`SLACK_RETENTION_DAYS`), tombstones anything
older than it. Both run at the internal rate limits.

## ACL

Slack has no computed permissions. The resolver answers "what may this person
read" from facts cached in `slack_channel_membership` and `slack_user`:

| Person | Readable indexed channels |
|---|---|
| Full member (`is_restricted=false`) | every indexed **public** channel of their workspace(s), joined or not, + indexed private channels they are a member of |
| Multi-channel guest (`is_restricted`) | only indexed channels they are a member of |
| Single-channel guest (`is_ultra_restricted`) | only that channel, if indexed |
| External Slack Connect user (`user_team` ≠ our team) | only indexed shared channels they are a member of |
| Deactivated (`deleted=true`) or unknown | nothing |
| App/bot users | nothing |

Membership comes from `conversations.members` when a channel is indexed and is
kept current by member and channel events, with a TTL refresh (default 6h).
On Enterprise Grid, a user's readable set is computed per workspace they
belong to (`users.info` / `team_id` on the event), and public channels count
only for workspaces the user belongs to. Any lookup error yields the empty set
for the affected channel. The ACL remains a SQL predicate over internal
channel ids.

## Audience

| Destination | Audience readable set |
|---|---|
| im with the bot | the asker's readable set; private |
| mpim | intersection of members' sets; GROUP_DIRECT, not private |
| private channel | intersection over `conversations.members` |
| public channel, no guests or external members | all indexed public channels of the workspace + private channels every member shares (usually none) — equivalent to intersecting over all full members |
| public channel with guests | intersection over members, which in practice is the destination only |
| Slack Connect (`is_ext_shared`) | the destination channel only |
| thread | same as its parent channel |

This matches Slack's own RTS rule for public and Connect channels.

## Commands

One umbrella command, `/cyberfriend` (renamable with `SLACK_COMMAND` because
slash commands are not namespaced), with subcommands from the catalogue:
`ask`, `channels`, `index`, `unindex`, `forget [here|everywhere]`,
`resolve <id>`, `notifications on|off`, `schedule create|list|delete`,
`alert list|delete`, `help`. Optional aliases (`/cf-ask`, `/cf-alert`, …) can
be declared in the manifest; they parse into the same invocation.

Flow: ack within 3 s with an ephemeral "working…" (or nothing), then reply
through `response_url` (ephemeral by default, max 5 uses / 30 min) or
`chat.postEphemeral`. Commands cannot run in threads, so thread-aware
behaviour (resolving an ask from its thread, asking about a thread) is also
available by @mentioning the bot in the thread and as a message shortcut
("Ask CyberFriend about this").

`/cyberfriend index`: public channel → `conversations.join` (needs
`channels:join`) then scope; private channel → the reply tells the requester
to `/invite @CyberFriend` first. Who may index: channel creator, workspace
admins/owners, or people listed in `SLACK_INDEX_MANAGERS` — Slack has no
per-channel "manage" permission, so `missing_permission_label` says
"workspace admin or channel manager".

## Confirmations and interactivity

Block Kit `actions` with Confirm/Cancel buttons carrying the choice-prompt id
in `value`; destructive buttons carry the `confirm` composition object.
`block_actions` is acked immediately; the handler checks `body.user.id`
against the prompt's requester (and team), then updates the message with
`chat.update` (or the `response_url` for ephemeral prompts). Forget-one-of-
several-wallets uses a static select plus a confirm button.

## Delivery

- Reply to a mention: in the thread of the mention (`thread_ts` = the
  message's thread or its own ts), public per audience rules.
- DM to a person: `conversations.open(users=U)` then `chat.postMessage`
  (`im:write`); `cannot_dm_bot`, `user_not_found`, `account_inactive` →
  `CLOSED`.
- Alerts, scheduled answers, obligation notifications: direct messages. 1
  message per second per channel is respected by the sender's per-channel
  limiter.
- Streaming (`chat.startStream`/`appendStream`/`stopStream`) and assistant
  status are optional progress indicators; plain `chat.update` of a progress
  note is the default.

## Formatting

`markdown_text` (standard Markdown, 12,000 chars) is the default for answers;
fixed replies and blocks use mrkdwn (`*bold*`, `_italic_`, `<url|label>`,
`&amp; &lt; &gt;` escaping, `<!here>`/`<!channel>`/`<!everyone>` defanged).
Beyond 12,000 characters the shared splitter produces several messages in the
same thread. Tables render as Markdown tables in `markdown_text`, as lists in
mrkdwn. Citations are permalinks from `chat.getPermalink` (cached) or built
as `https://<domain>.slack.com/archives/<C>/p<ts without dot>`.

## Scopes (bot token)

`app_mentions:read`, `channels:history`, `groups:history`, `im:history`,
`mpim:history`, `channels:read`, `groups:read`, `im:read`, `mpim:read`,
`users:read`, `chat:write`, `im:write`, `reactions:read`, `files:read`,
`files:write`, `commands`, `channels:join`; optional `users:read.email`
(email prefill only with consent), `assistant:write`.

## Federated mode (specified, not built)

`SlackNativeSearch` implements `SearchBackend` with
`assistant.search.context` using each viewer's user token (OAuth,
`search:read.public/private/im/mpim`, admin + user consent), 20 results per
page, a per-user limiter at 10/min, keyword search unless the workspace has
Slack AI Search. No message, window or embedding is persisted; an answer-time
cache is dropped when the answer is sent. Catch-up becomes an RTS query with
`after` and a channel filter; ask extraction runs only on messages addressed
to the bot; conversation memory keeps only the asker's own turns. Guests get
"not available". This mode cannot use Socket Mode if Marketplace-listed.

## Feature parity

| Feature | Discord | Slack (internal) | WhatsApp | Notes |
|---|---|---|---|---|
| Corpus answers + citations | yes | yes, permalinks | no (linked identity only) | federated: RTS, no storage |
| ACL | permissions_for | public/private/guest/Connect rules | n/a | fail closed |
| Audience-aware channel answers | yes | yes; Connect = channel only | n/a | |
| Asks + DM notifications | yes | yes; `<@U>` mentions, ✅ `reaction_added` | delivery only | |
| Conversation memory | yes | yes; thread = sub-place | 1:1 | |
| Personal facts DM-only | yes | im only; mpim not private | always 1:1 | |
| Language EN/PT | yes | yes; users.info locale hint | yes | |
| Documents | yes | file_share + url_private | 1:1 uploads | |
| Web/MCP federation | yes | yes | narrowed | |
| Market, wallets, DeFi, portfolio, activity | yes | yes | yes | formatting only |
| Alerts with confirm | View | Block Kit + confirm object | reply buttons / template | |
| Scheduled questions | yes | yes, DM | template | |
| Catch-up/digest | yes | yes | no | |
| /channels /index /unindex | yes | yes (join / invite) | no | |
| /forget /resolve /notifications /schedule /alert | slash | `/cyberfriend <sub>` + mention in threads | keywords | |
| Ephemeral replies | yes | yes (not persisted) | no | |
| Threads | yes | native, default reply target | no | |
| Edits/deletes | events | events + retention reconcile | /forget only | |
| Voice | no | clips via STT (later) | STT | |
| App Home / assistant pane | n/a | optional | n/a | |
| Admin, MCP, tracing | yes | + installs, ingest status | + number, costs | |
| e2e | DiscordWire | SlackWire | WhatsAppWire | |

## Open questions (answers change defaults, not structure)

1. Does the app stay internal? (Assumed yes; otherwise federated mode.)
2. Workspace plan (Pro / Business+ / Enterprise Grid)? Grid enables org-wide
   install; Business+ enables semantic RTS for the federated mode.
3. Socket Mode or HTTP on Coolify? (Default Socket Mode.)
4. Backfill depth on index? (Default 90 days.)
5. Slack Connect channels and guests in v1, or refuse to index Connect
   channels at first? (Default: supported with the stricter rules above;
   `SLACK_ALLOW_CONNECT=false` refuses indexing them.)
6. Umbrella command or separate commands? (Default umbrella + optional
   aliases.)
7. Cross-platform identity linking between Discord and Slack people? (Reuses
   the linking flow of `add-whatsapp-platform`.)
8. Clip transcription in v1? (Default: later, via the shared STT port.)
