## Why

The team's conversations are not only on Discord, and the original plan named
Slack as the next platform. After `add-platform-ports`, the core no longer
assumes Discord, so Slack can be an adapter. But Slack's rules decide the
architecture, not the other way round:

- **History limits depend on how the app is distributed.** Since 2025-05-29,
  `conversations.history`/`conversations.replies` are limited to 1 request per
  minute and 15 messages per page for new *commercially distributed,
  non-Marketplace* installs. Internal (customer-built, one organisation) apps
  keep Tier 3 (50+/min) and 1,000 per page.
- **Data terms depend on it too.** The Slack API terms (effective 2025-10-10)
  forbid apps offered outside their own organisation from keeping persistent
  copies, archives or indexes of other organisations' data, and forbid
  training models on API data. Internal apps are exempt.
- **Real-time Search (`assistant.search.context`)** is available only to
  Marketplace and internal apps; with a bot token it covers public content and
  needs an `action_token` that only mention events carry; private channels,
  DMs and mpims need a user token with admin and user consent; guests cannot
  use it; pages are 20 results at about 10 requests per minute per user.
- **Socket Mode apps cannot be Marketplace-listed**, and everything
  interactive must be acknowledged within 3 seconds.

CyberFriend's value is its own ACL-scoped index: catch-ups, asks, memory,
citations. That is allowed and practical only as an **internal app**. This
change therefore specifies Slack as an internal app for one organisation
(CyberdyneCorp's workspace or Enterprise Grid org) with full feature parity,
and specifies — but gates off — a federated mode for any later distributed
installation, so that decision does not require a redesign.

## What Changes

- **`adapters/slack/`** on `slack_bolt` (async) and `slack_sdk`:
  - Socket Mode transport by default (no public URL, fits Coolify), and an
    HTTP Events/Interactivity transport with signing-secret verification
    behind the same ingress, selected by `SLACK_TRANSPORT`. Every envelope is
    written to a `slack_inbound` table before it is acknowledged and drained
    by a worker, so an acked event survives a crash or deploy.
  - `SlackChatSource`: message events (including edits, deletes, thread
    replies, broadcasts, file shares), a rate-limit-aware newest-first
    backfill over `conversations.history` + `conversations.replies`, and a
    periodic reconcile (history and thread replies, aborting on any error)
    for workspaces with retention policies.
  - `SlackAclResolver` and `SlackAudienceResolver` over a membership cache,
    covering public and private channels, mpims, DMs, guests, Slack Connect
    and deactivated users, failing closed.
  - Bot surface: `/cyberfriend <subcommand>` umbrella command, @mentions and
    DMs with the bot, in-thread replies, Block Kit confirmations with the
    confirm composition object, ephemeral replies, direct messages through
    `conversations.open`, optional App Home and assistant-pane support.
  - `SlackRenderer` (mrkdwn and `markdown_text`), `SlackPermalinks`,
    `SlackMentionCodec`, `SlackAttachmentFetcher`, `SlackDirectMessenger`,
    `SlackChoicePrompt`, `SlackProgressIndicator`.
- **Mode switch `SLACK_MODE=internal|federated`** (default `internal`). Only
  `internal` is built in this change; `federated` is specified as a later
  optional phase with a `SlackNativeSearch` search backend that stores
  nothing.
- **Install management**: tokens stored per workspace/enterprise, an admin
  console page for Slack installs and per-channel ingest status, and an OAuth
  install flow for multi-workspace Grid installs.
- **Feature parity** requirements for every existing feature (see `design.md`).
- **e2e**: a `SlackWire` driving the Bolt app with signed payloads and a fake
  Web API, running the shared scenarios.

Non-goals:

- Marketplace listing or distribution to other organisations (the federated
  phase is specified, not built).
- Reading Slack content the bot is not a member of, or humans' DMs with each
  other.
- Training or fine-tuning on Slack data (forbidden by Slack terms in every
  mode; also never done on Discord).
- Slack Workflow Builder steps and Slack Canvas authoring.

## Capabilities

### New Capabilities

- `slack-ingestion`: installation, transports, what is captured and
  backfilled, rate limits, deletions and retention, and the deployment-mode
  rules about what may be stored.
- `slack-access-control`: who may read which Slack channel, audiences for
  channel replies, and the DM-only privacy rule on Slack.
- `slack-bot-surface`: commands, mentions, threads, ephemeral and direct
  replies, Block Kit confirmations, App Home, delivery of alerts, schedules
  and notifications, formatting, and per-feature parity.

### Modified Capabilities

None here. The existing requirements that were worded in Discord terms are
generalised by `add-platform-ports`; Slack satisfies them through its ports.

## Impact

- Depends on `add-platform-ports` (all of it). Age-based deletion uses the
  existing `app/retention.py` policy; there is no Slack-specific retention
  setting.
- New: `src/chatmemory/adapters/slack/*`, `entrypoints/slack.py` (exactly one
  Slack runtime process owns the Socket Mode connection and dispatches to the
  ingest and controller paths), `admin/handlers/slack.py`,
  `tests/e2e/harness/slack_wire.py`, a Slack app manifest in `deploy/slack/`.
- New dependencies: `slack_bolt`, `slack_sdk`.
- New migration: `slack_install` (per team/enterprise tokens, encrypted at
  rest), `slack_inbound` (persistent dedup and work queue), `slack_user`,
  `slack_channel_membership` cache, `slack_reconcile_state`.
- Settings: `SLACK_MODE`, `SLACK_TRANSPORT`, `SLACK_BOT_TOKEN`,
  `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_TEAM_ID` /
  `SLACK_ENTERPRISE_ID`, `SLACK_BACKFILL_DAYS`, `SLACK_RECONCILE_DAYS`,
  `SLACK_COMMAND`, `SLACK_INDEX_MANAGERS`, `SLACK_ALLOW_CONNECT`,
  `SLACK_APP_HOME`,
  `SLACK_CLIENT_ID`/`SLACK_CLIENT_SECRET` (OAuth only).
- Docs: `docs/slack-setup.md`, `docs/operations.md`, README.

## Risk

Slack is where a disclosure is easiest to cause: public channels are readable
by every full member whether they joined or not, guests and Slack Connect
members are not, and mpims look like DMs. The ACL rules below are the
highest-risk part and carry the most regression tests. The second risk is
policy: running the internal-app design as a distributed app would break
Slack's data terms, so the mode is an explicit setting, the internal mode
refuses a second workspace outside the configured organisation, and the
federated mode's no-storage rule is a requirement rather than a convention.
