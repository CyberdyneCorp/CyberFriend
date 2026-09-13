## Why

Everything built so far is unreachable from Discord. The corpus, the permission filter, the reasoning loop and the federated tools are real, but the only way to reach them is an MCP client — an engine with no steering wheel. This change adds the surface people actually use: mention the bot, DM it, or run a slash command, and get an answer.

It also settles a question the earlier changes deliberately left open. Answers were specified as delivered "only to the requester", which is safe but antisocial: nobody else benefits, and the bot never becomes part of how the team works. The decision here is that **answers are public in the channel by default** — which is only safe if the answer is scoped to what that channel's audience may see, rather than to what the person asking may see. That scoping is the substance of this change.

## What Changes

- Respond to mentions, direct messages, and slash commands, and to replies within a conversation the bot is already part of.
- Answer publicly in the channel by default, scoped to the channel's audience.
- Scope a public answer to evidence readable by everyone who can read the channel it is posted in, not by the person who asked.
- Tell the asker privately when their own access would have produced a fuller answer, without disclosing anything to the channel.
- Keep multi-turn context within a thread or DM so follow-up questions work.
- Apply per-person rate and cost limits, since each question can trigger a reasoning run.
- Show progress on long-running questions and stay responsive within Discord's interaction deadlines.

Non-goals for this change:

- Voice channels, image understanding, and message-content generation on a user's behalf.
- Acting autonomously or on a schedule. Every answer follows a question.
- Slack.

## Capabilities

### New Capabilities

- `discord-bot-surface`: how people address CyberFriend from Discord — mentions, DMs, slash commands, threaded follow-ups, progress, and limits.
- `answer-disclosure`: determining the audience of an answer and scoping its evidence to that audience, so that posting publicly never discloses what a private delivery would not.

### Modified Capabilities

None. `agent-authorization`'s existing requirement that answers reach only the requester is written against a private delivery model; this change introduces public delivery as a distinct, audience-scoped mode rather than altering that requirement. The two are reconciled in `answer-disclosure`, which is stricter, not looser: an answer's evidence is bounded by its audience in both modes.

## Impact

- **Depends on** `add-discord-chat-memory` for retrieval and permissions, and on `add-agentic-reasoning-and-mcp-federation` for producing answers.
- **Identity comes free.** In Discord the asker is authenticated by Discord itself — no bearer token, no client-supplied viewer parameter. This surface is structurally safer than the public MCP endpoint and is the better default way to reach CyberFriend.
- **New cost exposure.** Anyone in the server can now trigger a reasoning run. Rate and cost limits are requirements here, not tuning.
- **Audience scoping needs a new computation**: for a candidate source channel, whether everyone who can read the destination channel can also read it. This is derivable from the role and overwrite data the permission resolver already handles, but it is a different question from "may this person read this channel".
- **Public answers change the disclosure surface.** Before this change a retrieval defect exposed content to one person; after it, to a channel. The audience-scoping rule exists to keep that from being a regression.
