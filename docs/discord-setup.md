# Preparing Discord for CyberFriend

Everything you need from Discord, in order. Takes about ten minutes.

You will end up with three values:

| Value | Looks like | Used for |
|---|---|---|
| `DISCORD_TOKEN` | `MTIzNDU2...` (~70 chars) | Logging the bot in |
| `DISCORD_GUILD_ID` | `1013238409123456789` | Which server to index |
| `INDEXED_CHANNEL_IDS` | `1013238409123456789 1013238409987654321` | Which channels to index |

---

## 1. Create the application

1. Go to <https://discord.com/developers/applications> and sign in.
2. **New Application**, name it (`CyberFriend`), accept the terms, **Create**.

An "application" is the container. The bot lives inside it.

## 2. Add the bot and copy the token

1. Open the **Bot** tab in the left sidebar.
2. **Reset Token** → confirm → **Copy**.

Paste it somewhere safe *now*. Discord shows a bot token exactly once; if you
lose it you reset it again, which invalidates the old one and logs out any
running instance.

> **The token is a password for your bot.** Anyone holding it can act as the
> bot in every server it has joined. Never commit it, never paste it into a
> channel, never put it in a screenshot. If it leaks, reset it immediately —
> that is the whole remediation, and it is instant.

## 3. Enable both privileged intents

Still on the **Bot** tab, scroll to **Privileged Gateway Intents** and turn on:

- **MESSAGE CONTENT INTENT**
- **SERVER MEMBERS INTENT**

Leave **PRESENCE INTENT** off — CyberFriend never uses it.

Both are required, for different reasons:

**MESSAGE CONTENT** is what lets the bot read what people actually wrote.
Without it the bot still receives message events, but `content` arrives empty,
so the corpus fills with blank messages and retrieval returns nothing.

**SERVER MEMBERS** is what lets the bot enumerate who can read a channel. That
is how it decides what a public answer may cite: an answer posted in a channel
may only quote sources that *everyone able to read that channel* can also
read. Working that out from role lists alone gets the important cases wrong,
because a member individually denied a channel still reads the room.

Without SERVER MEMBERS the bot **fails closed**: audiences resolve to empty and
public answers cite nothing at all. If the bot replies but never cites a
source, check this first.

### Do I need Discord's approval?

Almost certainly not. Since **2026-06-10** the review threshold is **10,000
unique users who can see your app**, not the old 100-server rule. For one
internal server these are just toggles.

## 4. Invite the bot to your server

Open **OAuth2 → URL Generator**:

- **Scopes:** `bot` and `applications.commands`
- **Bot permissions:** View Channels, Send Messages, Send Messages in Threads,
  Read Message History, Use Application Commands

Copy the generated URL, open it, pick your server, authorise.

Or build the URL directly — replace `YOUR_APP_ID` with the **Application ID**
from the **General Information** tab:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&permissions=277025459200&scope=bot%20applications.commands
```

`277025459200` is exactly the five permissions above and nothing else.

> **Do not grant Administrator.** It is tempting and it is wrong. CyberFriend's
> whole design is that it can only surface what the person asking is already
> allowed to read; Administrator makes the bot able to read every channel, so
> anything it indexes becomes reachable. Grant it the channels you intend to
> index, and nothing more.

## 5. Turn on Developer Mode, then copy the IDs

**User Settings → Advanced → Developer Mode**, on.

- **Server ID:** right-click the server name in the sidebar → **Copy Server ID**
  → this is `DISCORD_GUILD_ID`.
- **Channel IDs:** right-click a channel → **Copy Channel ID** → these are
  `INDEXED_CHANNEL_IDS`, separated by spaces or commas.

Start with **one** channel. Indexing is opt-in and `INDEXED_CHANNEL_IDS` is
empty by default, precisely so that nothing is archived by accident. Widen it
once you have seen the bot behave.

## 6. Check the bot can see the channel

In each channel you plan to index, confirm the bot appears in the member list.
If a channel has permission overwrites, add the bot's role explicitly and give
it **View Channel** and **Read Message History**.

`Read Message History` is easy to miss: without it the bot sees new messages
arrive but cannot backfill anything said before it joined, and the channel is
treated as unreadable for permission purposes.

## 7. Hand the values over

Put them in `.env.local` at the repository root — gitignored, never committed:

```bash
DISCORD_TOKEN=paste-the-token-here
DISCORD_GUILD_ID=1013238409123456789
INDEXED_CHANNEL_IDS=1013238409987654321
```

For the Coolify deployment the same three values go in the application's
environment variables at **runtime** scope. See [deploy-coolify.md](deploy-coolify.md).

---

## What the bot cannot do, by design

Worth knowing before anyone expects otherwise.

**It cannot read direct messages between people.** No bot on any platform can.
"What did people ask me today" covers indexed channels and DMs sent *to the
bot*, and nothing else.

**It does not index private threads.** A private thread's members are a
narrower set than the parent channel's readers, and Discord derives visibility
from channel overwrites, which say nothing about thread membership. Indexing
one would make a private conversation readable by everyone who can read the
parent, so those threads are skipped entirely.

**It never posts unprompted.** Every answer follows a question.

---

## Before you point it at a real server

Adding the bot creates a permanent, searchable archive of everything said in
the channels you index. That is the feature, and it is also a decision your
team should make knowingly rather than discover.

- Tell people the bot is being added and which channels it reads.
- Agree a retention window.
- Know that per-person opt-out exists, and how someone asks for it.

Starting with one low-stakes channel makes all of this easier to reverse.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `LoginFailure: Improper token has been passed` | Token wrong, or reset after you copied it. Reset and copy again. |
| Bot online, ignores everything | `MESSAGE CONTENT INTENT` off — message content arrives empty. |
| Bot answers but never cites anything | `SERVER MEMBERS INTENT` off — audiences resolve empty, so it fails closed. |
| `PrivilegedIntentsRequired` on startup | An intent is enabled in code but not in the portal. |
| Bot cannot see a channel it should | Channel overwrites. Grant its role View Channel **and** Read Message History. |
| Nothing is indexed, no errors | `INDEXED_CHANNEL_IDS` empty. Indexing is opt-in; startup logs a warning. |
| Answers omit things you can see | Working as intended. A public answer is limited to what the whole channel may read — ask in a DM for your full view. |
