# CyberFriend

An ACL-aware chat-memory agent for Discord. It indexes channel history into
Postgres with pgvector and answers questions about it — *what did people ask me
today*, *what did we decide about the deploy* — filtered to the channels the
person asking is actually permitted to read.

When your own conversations do not hold the answer, it can look outside: the
web, a federated MCP server, or live market data. Anything from outside is
labelled as such, so you can always tell what a colleague said from what the
internet said.

## What it does

| | |
|---|---|
| **Answers from your channels** | Hybrid lexical and vector retrieval over conversation windows, scoped to what you may read, with citations that link back to the message |
| **What someone said** | *o que o João disse sobre o deploy semana passada?*, *what did Ana say about pricing yesterday?*, *o que eu falei sobre X?*, *@Maria comentou algo sobre Y ontem?* — only that person's own messages, from channels you and the room can read, cited. Days and weeks are calendar ones in `ANSWER_TIMEZONE`. *o que o Leo falou com você hoje?* works too. Two Joãos get *Qual João?*; a name nobody visible goes by is answered as an ordinary question. People archived before names were recorded are named from the member list when ingest starts, so they can be asked about by name and are named in citations |
| **Audience-aware answers** | In a channel it only cites what everyone there can read; ask in a DM for your full view |
| **Catch-up** | *o que eu perdi no #general?*, *what did I miss in #infra this week?* — a cited digest of one channel for the period you name (since yesterday otherwise), in sections: decisions, open questions, requests, other highlights. A typed `#name` works when one channel has it. `/schedule` it for a daily digest. A channel you or the room cannot read gets one refusal that says nothing about why |
| **Decisions** | *o que decidimos sobre o deploy?*, *o que ficou decidido semana passada?*, *what did we decide about pricing?* — read from conversation as it arrives, answered from the decision log with no model call, dated and newest first, each line citing the message that settled it. Only decisions from channels you and the room can read; deleting the conclusion or the proposal it settled removes it. No match is answered as an ordinary question, never as "nothing was decided" |
| **Obligations** | Extracts what people asked of each other. `what do I need to do?`, closed with a ✅ reaction or `/resolve`; a DM when someone asks you for something, switched with `/notifications` |
| **Conversation memory** | Follow-ups keep context, per person and per place, and stop being recalled if you lose access to a channel behind them |
| **Personal facts** | *my name is …*, *call me Leo*, *my email is …*, *my phone is …*, *moro em …*, *nasci em …*, *my wallet is 0x…* / *bc1…*, *reply in Portuguese*, *prefiro ver em reais*. Contact details and wallets are only ever shown in a DM to you |
| **Your currency beside USD** | Say *minha moeda é o real*, *my currency is euro* or *moeda preferida: BRL* — or *uso reais* in an introduction — and every dollar figure (BTC/ETH prices, balances, pools, Aave, portfolio totals, wallet activity, price and health alerts) also shows in that currency: *US$ 63.210,00 (R$ 345.126,60)*. Converted in code at the daily ECB reference rate; no rate, dollars only |
| **Knows the time** | Every answering prompt carries the current date and time in UTC, so *today* and *recent* mean something |
| **Answers in your language** | An answer is written in the language you asked in, and fixed replies come in Portuguese or English to match; a saved preferred language still wins |
| **Documents** | Attachments and linked documents, parsed in a sandboxed child process |
| **Web and MCP** | Wikipedia, Google via SerpApi, and any MCP server an operator allowlists |
| **Market data** | BTC and ETH, the S&P 500, and currency conversion, each stated with how current it is |
| **Wallet balances** | What a `0x` address holds on Ethereum, Base and Arbitrum, with USD values. An address you typed, or the one you saved |
| **DeFi positions** | Open Uniswap v3/v4 liquidity positions (pair, value, in/out of range, min/max price, uncollected fees) and Aave v3 supplies, borrows and health factor, on the same three chains |
| **Wallet activity** | *o que essa carteira fez essa semana?*, *what did my wallet do this week?*, *minhas transações de ontem* — swaps, Uniswap liquidity, Aave supplies and borrows, transfers and gas, per chain, newest first, for up to 30 days. Relayed (EIP-7702) actions included; poisoning spam hidden and flagged; counterparties in full in a DM, redacted in a channel |
| **Portfolio total** | *quanto eu tenho no total?*, *what's my portfolio worth?*, *show my portfolio*, *me mostra o meu portfólio* — balances, pools with uncollected fees and the Aave net, per chain and per wallet, summed in USD. Your saved wallet plus any you type; "at least" when a chain could not be read |
| **Alerts** | Ask *tell me when my LP goes out of range*, *warn me when my LP is within 5% of the range edge*, *me avisa se o health factor cair abaixo de 1,3* or *avisa quando o BTC passar de 100k*. A Confirm button shows exactly what will be watched and where it is now, and a DM arrives once when it changes. `/alert list`, `/alert delete`. Off by default |
| **Index from chat** | `/index #channel` for anyone with Manage Channels there, applied without a redeploy |
| **See what is archived** | `/channels` lists the archived channels you can read, and discloses nothing about the rest |
| **See what it holds about you** | `/privacy` lists everything stored about you: facts, remembered conversation, scheduled questions, alerts, notification setting, voice minutes, attachments on your archived messages, suggestions, access tokens, archived message counts in the channels you can read now, and how many of your questions are traced. In a server channel it is private and shows counts and fact kinds only, with a button that sends the values by DM; in a DM it shows the values. It states how long questions and answers are traced (`TRACE_RETENTION_DAYS`), that admins can read them, and what survives a deletion, including backups (`BACKUP_RETENTION_DAYS`) |
| **Delete everything about you** | `/privacy` -> **Delete everything…** erases your messages and their attachments, facts, memory, tasks, alerts, notifications, suggestions, tokens and voice usage records, and schedules your traced questions for deletion, after you type `DELETE` (`APAGAR` in Portuguese). Choose to keep using the bot (new messages are archived, nothing older is re-imported) or to also stop being archived. It finishes even if the bot restarts midway, and says what is kept (see `docs/operations.md`) |
| **Scheduled questions** | `/schedule` asks something for you hourly to daily and messages you the answer — only when there is one. Off by default |
| **Feature requests** | `/suggest` records an idea in your own words (up to 1000 characters) and answers with its number; `/suggestions` lists yours and their status. A message starting "tenho uma sugestão", "sugestão:", "seria legal se você", "I have a feature request", "feature request:" or "it would be nice if you could" is offered with [Record suggestion] and [No, answer it], unless something else the bot does answers it. The team sees the text and your Discord name. Text with an email, phone number or wallet address is refused, resubmitting is idempotent, and five a day at most. Opt-out deletes them |
| **Voice questions** | Send the bot a voice message in a DM and get an answer as if you had typed it, with a small quoted line of what it understood. Transcribed by `gpt-4o-mini-transcribe`; the audio is never stored. Hard monthly caps per person and for the server. Off by default |
| **Channel media (recording only)** | From `MEDIA_ENABLED_AT`, voice notes and images posted in indexed channels are recorded as pending rows: metadata and a CDN link, nothing downloaded and nothing searchable yet. Transcribing voice notes and reading images come later. Off by default |
| **Admin console** | A web console for federation, channels, retention, opt-outs and tokens |
| **MCP interface** | Your corpus as an MCP server, under the same permission rules |
| **Tracing** | Each answer — question, answer, feature, references to the evidence behind it (never its text), per-call model token usage and federated tool calls (never their arguments) — exported to Langfuse for study. Off by default. When on, each person's first traced reply carries a one-time notice (EN/PT) that questions and answers are recorded for up to `TRACE_RETENTION_DAYS` days, admins can read them, and `/privacy` shows how many are kept and deletes them; "what can you do?" says the same |

### Every feature at a glance

```mermaid
mindmap
  root((CyberFriend))
    Your channels
      Answers with citations
      Scoped to what you can read
      Audience-aware in channels
      Documents and links
      Catch-up on a channel
      o que eu perdi no #general
      Daily digest with /schedule
      What someone said, and when
      o que o João disse semana passada
      What we decided, dated and cited
      o que decidimos sobre o deploy
    What you owe
      Asks extracted from chat
      what do I need to do
      Close with a reaction or /resolve
      DM when someone asks you
    Memory
      Follow-ups keep context
      Chain and price answers remembered
      /forget here or everywhere
    About you
      Full and preferred name
      Email, phone, address, birth date, DM only
      ETH and BTC wallets
      Several facts in one message
      Replies in your language
      Figures in your currency beside USD
      Knows the date and time
      Voice questions in a DM, off by default
    Outside the server
      Wikipedia and Google
      Allowlisted MCP servers
      BTC, ETH, S&P 500, FX
    On-chain
      Ethereum, Base, Arbitrum
      Wallet balances with USD
      Open Uniswap v3 and v4 positions
      Range, fees, in or out of range
      Aave supplies, borrows, health factor
      Portfolio total per chain and wallet
      quanto eu tenho no total
      Wallet activity: swaps, LP, Aave, transfers, gas
      o que essa carteira fez essa semana
      Address-poisoning spam hidden and flagged
      Alerts: LP out of range or near its edge
      Alerts: health factor below a limit, BTC or ETH past a price
      Asked in words, created with a Confirm button
    Commands
      /ask /channels /forget
      /resolve /notifications
      /schedule create, list, delete
      /alert list, delete
      /suggest /suggestions
      /privacy
      In the server and in DMs
      /index /unindex in the server
    Operators
      Index a channel from chat
      Admin console
      MCP interface to the corpus
      Langfuse tracing
      Retention and opt-outs
      Channel media recorded, off by default
```

### What you can ask for

```mermaid
graph LR
    P(("You"))

    P --> CORPUS["Your channels"]
    CORPUS --> C1["o que decidimos sobre o deploy? &middot; what did we decide about pricing?"]
    CORPUS --> C2["o que eu perdi no #general? &middot; what did I miss in #infra"]
    CORPUS --> C3["/ask &middot; /channels"]
    CORPUS --> C4["o que o João disse sobre o deploy semana passada?"]

    P --> OWED["What you owe"]
    OWED --> O1["what do I need to do"]
    OWED --> O2["/resolve &middot; check reaction"]
    OWED --> O3["a DM when someone asks you for something"]

    P --> OUT["Outside the server"]
    OUT --> X1["Wikipedia &middot; Google"]
    OUT --> X2["BTC &middot; ETH &middot; S&P 500 &middot; currencies"]
    OUT --> X3["wallet balances on Ethereum &middot; Base &middot; Arbitrum"]
    OUT --> X5["my LP positions &middot; my Aave health factor"]
    OUT --> X6["quanto eu tenho no total? &middot; what's my portfolio worth?"]
    OUT --> X7["o que essa carteira fez essa semana? &middot; what did my wallet do this week?"]
    OUT --> X4["any MCP server an operator allowlists"]

    P --> YOU["About you"]
    YOU --> Y1["call me Leo &middot; my email is ... &middot; my phone is ..."]
    YOU --> Y2["my wallet is 0x... then: my wallet balance?"]
    YOU --> Y3["answers in the language you asked in"]
    YOU --> Y5["prefiro ver em reais &middot; prices and totals in USD and BRL"]
    YOU --> Y4["/forget &middot; /notifications &middot; /privacy"]
    YOU --> Y6["/suggest an idea &middot; /suggestions"]
    YOU --> Y5["a voice message in a DM, answered as typed"]

    P --> WHEN["On a schedule"]
    WHEN --> W1["/schedule create, hourly to daily"]
    WHEN --> W2["/schedule list &middot; delete"]
    WHEN --> W3["a DM only when there is something &middot; a daily digest of #general"]
    WHEN --> W4["tell me when my LP goes out of range &middot; Confirm"]
    WHEN --> W5["/alert list &middot; delete"]

    style P fill:#C8E6C9,stroke:#2E7D32
    style CORPUS fill:#E3F2FD,stroke:#1565C0
    style OWED fill:#E3F2FD,stroke:#1565C0
    style OUT fill:#FFF9C4,stroke:#F9A825
    style YOU fill:#E3F2FD,stroke:#1565C0
    style WHEN fill:#F3E5F5,stroke:#6A1B9A
```

Yellow is everything that leaves the server, and all of it is off until an
operator turns it on. Purple is what messages you without being asked in the
moment: scheduled questions and position alerts.

### Commands

| | |
|---|---|
| `/ask` | Ask about what's been said |
| `/channels` | The archived channels you can read |
| `/index`, `/unindex` | Archive a channel, or stop and delete the archive |
| `/forget` | Erase what I remember of our conversation |
| `/resolve` | Close something I said was asked of you |
| `/notifications` | Turn DMs about obligations on or off |
| `/schedule create`, `list`, `delete` | Questions asked on a rhythm |
| `/alert list`, `delete` | Your alerts (range, range edge, health factor, BTC/ETH price), and stopping one. An alert is created by asking in words and pressing Confirm |
| `/suggest` | Suggest something the bot should learn to do; the reply gives its number and asks whether to DM you when its status changes |
| `/suggestions` | Your suggestions and their status (new, triaged, planned, done, declined, duplicate) |
| `/privacy` | What I hold about you and what is kept after a deletion. Counts and fact kinds in a server channel (only you see it, with a button for the details by DM); values in a DM. **Delete everything…** erases it all, with or without opting you out, after a typed confirmation |

Every command except `/index` and `/unindex` works in the server **and in a
direct message with the bot**; those two act on a channel, so they live in the
server only. `/notifications`, `/schedule` and `/alert` are described only where
those features are switched on — a command Discord will not show you is worse than one that is
missing from this table.

### What it can remember about you

Tell it yourself, in your own message. It never learns these from a channel.

| | |
|---|---|
| Full name | `my name is Leonardo Araujo` |
| Preferred name | `call me Leo` |
| Email | `my email is leo@example.com` |
| Phone | `my phone is +55 11 99999 1234` |
| Home address | `I live in …` / `moro em …` |
| Birth date | `I was born on 21/06/1981` / `nasci em 21 de junho de 1981` |
| Preferred language | `reply to me in Portuguese` |
| Preferred currency | `my currency is euro` / `prefiro ver em reais` / `moeda preferida: BRL` |
| Ethereum wallets (up to 5) | `my wallet is 0x…` |
| Bitcoin wallets (up to 5) | `my btc wallet is bc1…` |

You can give several at once — *"me chamo Leonardo Araujo dos Santos, pode me
chamar de Leo, nasci em 21/06/1981, meu telefone é …, moro em …, minha carteira
é 0x…, meu email …"* — and the reply lists what was saved and what was not. An
age is not kept: it follows from the birth date. "My name is" with one word is
the name you're called by; with more, it is your full name. Common typos
("walet", "morro") and a missing "é"/"is" before an email, phone or wallet are
understood.

`what do you know about me?` (or `o que você sabe sobre mim?`) shows them,
`what's my phone?` / `qual o meu telefone?` shows one, and `forget my email`
deletes one. Replies come in the language you wrote in.

Saving another wallet adds it rather than replacing the first. `forget my
wallet 0x…` (or `…45e0`) / `esqueça minha carteira 0x…` removes that one;
with several saved, `forget my wallet` alone asks which. `forget my wallets` /
`esqueça minhas carteiras` removes them all, Ethereum and Bitcoin. `what's my portfolio?`
sums every saved wallet; a balance, DeFi, activity or alert question reads one, so with
several saved it asks which (by their last four characters) unless you name it —
`what's my wallet balance …45e0?`.

With a preferred currency saved, every dollar figure the assistant reads for
you is followed by the same figure in that currency, converted in code at the
day's ECB reference rate (a footnote names the rate): prices, balances, pools,
Aave, portfolio totals, wallet activity and alert messages. The currencies are
the ones that rate is published for (AUD, BRL, CAD, CHF, CNY, EUR, GBP, JPY,
MXN, … — an unsupported one such as the Argentine peso is refused with the
list); US dollars as the preference means no second figure. If the rate
cannot be read, the answer is in dollars alone. It is not private: like the
language, it is shown in a channel too. `forget my currency` / `esqueça minha
moeda` removes it, and saying another replaces it.

In a direct message the assistant can also use your email, phone, address,
birth date and wallets when answering you; in a channel it never sees them.

**Your email, phone, address, birth date and wallets are only ever shown to
you, in a direct message.** A wallet is public on its chain — what is private
is that it is *yours*, and naming it in a channel makes that link for everyone
present. A channel message giving your own email, phone, address or birth date
is never archived, even when it says other things too.

Once a wallet is saved, `what's my balance?` or `what's my wallet balance?` uses
it instead of asking for an address, and `quanto eu tenho no total?` adds up
everything it holds. A longer question has to name a wallet: "my balance of
vacation days" stays with your channels.

## The rules it keeps

These are the invariants the whole design is arranged around. They are specified
in `openspec/` and tested, not left to a prompt.

- **Retrieval is scoped in SQL, never filtered afterwards.** The viewer is a
  required argument; a permission predicate that runs after ranking silently
  under-returns for whoever is in fewest channels.
- **Retrieved content is data, never instruction.** Messages, documents, tool
  results, remembered turns and nicknames are fenced with an unpredictable
  delimiter and neutralised.
- **Answers are grounded.** Every claim traces to retrieved evidence; the
  assistant says it found nothing rather than answering from the model's own
  knowledge. Memory interprets a follow-up but never becomes a source.
- **Deleted content disappears everywhere, immediately.**
- **What leaves is only what you typed.** An outbound query must be rooted in
  the asker's own words, so retrieved content cannot become a search term. A
  value you saved about yourself — your own wallet — counts as your words,
  checked against what the store actually holds for you rather than against a
  label anybody can apply.
- **State-changing tools need a person's approval**, with the exact arguments
  shown.

## Architecture

Three long-running processes over one database, plus a one-shot migration and
the console.

```mermaid
graph TD
    D["Discord gateway"] --> ING["ingest"]
    ING --> PG[("Postgres + pgvector")]
    ING --> EMB["Embeddings API"]
    ING -.->|"deletes traces of<br/>deleted messages"| LF["Langfuse"]

    D --> BOT["bot"]
    BOT --> PG
    BOT --> LLM["Chat model"]
    BOT --> EXT["Web · MCP · market · chain"]
    BOT -.->|"question, answer,<br/>evidence"| LF
    BOT --> SCH["scheduled tasks<br/>sweep"]
    SCH --> PG

    MCPS["mcp"] --> PG
    ADM["admin console"] --> PG
    ADM --> OP["Operator"]
    ADM -->|"sign-in (OIDC)"| CA["CyberdyneAuth"]
    MIG["migrate"] --> PG

    style PG fill:#E3F2FD,stroke:#1565C0
    style BOT fill:#C8E6C9,stroke:#2E7D32
    style EXT fill:#FFF9C4,stroke:#F9A825
    style LF fill:#F3E5F5,stroke:#6A1B9A
```

Dotted edges are tracing, and they go both ways for a reason: the bot exports
what a run saw, and `ingest` deletes those exports when the message they quote
is deleted. Without the second edge the first would quietly break the
guarantee that deleted content disappears everywhere.

| Process | Role | Public |
|---|---|---|
| `migrate` | Applies migrations once per deploy, then exits | no |
| `ingest` | Capture, backfill, windowing, embeddings, ask and decision extraction, media rows, naming people, retention | no |
| `bot` | Answers questions in Discord | no |
| `mcp` | MCP interface to the corpus | yes |
| `admin` | Operator console and its API | yes |

`ingest` runs exactly one replica. Two containers on one bot token both identify
to the gateway and ingest every message twice; Discord does not complain, the
corpus just silently doubles.

### How a question is answered

```mermaid
flowchart TD
    Q["Question"] --> ROUTE{"What kind of<br/>question is it?"}

    ROUTE -->|"what can you do"| SELF["Answered from<br/>configuration"]
    ROUTE -->|"a price, a wallet,<br/>pools or loans"| LIVE["Chain and market tools"]
    ROUTE -->|"anything else"| ACL["Resolve what this<br/>person may read"]

    ACL --> RET["Retrieval,<br/>scoped in SQL"]
    RET --> ENOUGH{"Does the evidence<br/>answer it?"}
    ENOUGH -->|yes| ANS["Answer, labelled<br/>by where it came from"]
    ENOUGH -->|no| OUT["Web or MCP, rooted<br/>in the asker's words"]
    OUT --> ANS

    SELF --> ANS
    LIVE --> ANS
    ANS --> TRACE["Recorded to Langfuse"]

    style ROUTE fill:#FFF9C4,stroke:#F9A825
    style RET fill:#E3F2FD,stroke:#1565C0
    style ANS fill:#C8E6C9,stroke:#2E7D32
```

**The first branch is the one worth understanding.** Some questions must never
reach the corpus, because the corpus cannot hold their answer and will confidently
supply a wrong one instead. Asked what it could do, the assistant once replied
with a colleague's project description, read out of a channel; asked for a wallet
balance, it reported that "the project can know balance information". Both were a
tangentially-related message winning because it was retrieved first.

So the route is decided before retrieval, and only what is left goes to the
corpus. Everything that does is scoped in SQL by the asking person's own
readable channels — never filtered afterwards.

Every answer says where it came from, and the three routes differ in what that
means: a corpus answer carries citations that link back to the messages, an
external one is labelled as from outside the server, and a self-description
carries neither — nothing in it came from a message, and inventing a source for
a description of configuration would make it look retrieved.

The code follows the same shape: `domain/` holds the vocabulary, `app/` the
rules, `adapters/` everything that talks to Discord, Postgres, models and the
web, and `entrypoints/` wires each process together. `composition.py` is the one
place that knows how the parts fit.

## Running it locally

You need Docker, Python 3.11+, [uv](https://docs.astral.sh/uv/),
[just](https://just.systems) and Node (for the console).

```bash
just install          # virtualenv, dependencies, console packages
cp .env.example .env  # fill in DISCORD_TOKEN, DISCORD_GUILD_ID, LLM_API_KEY
just up               # Postgres with pgvector
just migrate
just run-ingest       # in one terminal
just run-bot          # in another
```

Setting up the Discord side — application, token, intents, invite, and the
server and channel IDs — is walked through in
[docs/discord-setup.md](docs/discord-setup.md). Two privileged intents are
required:

- **MESSAGE CONTENT**, to read what people actually said. Without it every
  message arrives empty.
- **SERVER MEMBERS**, to work out who can read a channel. Without it the bot
  fails closed: audiences resolve empty and public answers cite nothing.

`INDEXED_CHANNEL_IDS` is empty by default and indexes nothing, because an
archive is a decision rather than a default.

### Everyday commands

`just` on its own lists them all.

| | |
|---|---|
| `just check` | Everything CI runs: lint, types, tests, specs |
| `just test` | Unit and integration, migrating first |
| `just test-unit` | No database, a few seconds |
| `just t tests/unit/test_memory.py -k recall` | One file or one test |
| `just goldens` | Retrieval quality measurement (spends embedding calls) |
| `just db-reset` | Back to a clean database |
| `just build` | The production image, as the platform builds it |
| `just admin-token leonardo` | A console credential for one operator |
| `just mcp-token 123456789` | An MCP credential bound to one Discord account |
| `just decisions-backfill --since 2026-06-01 [--until YYYY-MM-DD]` | Re-extract older history for decisions (paid; see docs/operations.md first) |

## Tests

```bash
just check
```

Integration tests need the live pgvector database and skip when none is
reachable, so a missing database is never reported as a defect in the code. The
retrieval golden set is opted into separately because it spends embedding
calls; see [tests/evaluation/BASELINE.md](tests/evaluation/BASELINE.md) for the
recorded baseline and how to read it.

## Deployment

Runs on Coolify as a `dockercompose` resource against a managed **PostgreSQL
with pgvector** database; the stock `postgres` image does not carry the
extension. [docs/deploy-coolify.md](docs/deploy-coolify.md) has the full
runbook, including three settings that have each broken this deployment once:

- **Connect To Predefined Network** must be on, or nothing reaches the database.
- **Every setting must appear in `docker-compose.yml`**, or the platform refuses
  it and the operator silently gets the default.
- **`CHAT_MODEL_CAPABILITIES` narrows** what the model is assumed to support, so
  omitting one turns it off.

Do not trust a green status on its own. To confirm the bot really reconnected
after a deploy, check that Discord's IDENTIFY count dropped:

```bash
curl -s -H "Authorization: Bot $DISCORD_TOKEN" \
  https://discord.com/api/v10/gateway/bot | jq .session_start_limit.remaining
```

## Configuration

Settings are environment variables, read at startup; indexing scope is also
read live from the database so it can change without a redeploy. `.env.example`
lists the common ones. The settings worth knowing:

| | |
|---|---|
| `INDEXED_CHANNEL_IDS` | What is archived. Empty means nothing |
| `CHAT_MODEL`, `EXTRACTION_MODEL` | Answering and the cheap background work |
| `CHAT_MODEL_CAPABILITIES` | What the endpoint supports; narrowing is deliberate |
| `WEB_TOOLS_ENABLED`, `SERPAPI_KEY` | Wikipedia and Google. Off by default |
| `MARKET_TOOLS_ENABLED` | BTC, ETH, S&P 500, currency conversion. Off by default |
| `WALLET_TOOLS_ENABLED`, `INFURA_KEY` | Wallet balances and DeFi positions on Ethereum, Base and Arbitrum. Off by default |
| `POSITIONS_TIMEOUT_SECONDS` | Per-chain bound for liquidity and Aave lookups (default 25) |
| `FEDERATION_SERVERS`, `FEDERATION_TOOL_ALLOWLIST` | MCP servers and the tools allowed from them |
| `MEMORY_RETENTION_DAYS` | How long conversation memory is kept |
| `ANSWER_TIMEZONE` | The calendar "ontem" and "last week" are read in (IANA name, default `America/Sao_Paulo`) |
| `ASK_EXTRACTION_ENABLED` | Whether obligations and decisions are extracted |
| `DECISION_MIN_SIMILARITY` | How close a stored decision must be to the question's topic to be listed (cosine, default 0.4); below it the question is answered by retrieval. |
| `SCHEDULED_TASKS_ENABLED` | Questions asked on a schedule. Off by default |
| `ALERTS_ENABLED`, `ALERT_SWEEP_SECONDS` | Alerts (range, range edge, health factor, BTC/ETH price), created by asking and confirming. Off by default, and needs `INFURA_KEY` |
| `VOICE_QUESTIONS_ENABLED`, `MEDIA_API_KEY` | Voice messages in a DM, transcribed at `MEDIA_BASE_URL` with `MEDIA_AUDIO_MODEL`. Off by default; enabling without a key stops the bot at boot |
| `VOICE_PERSON_MONTHLY_MINUTES`, `MEDIA_AUDIO_MONTHLY_MINUTES` | Hard monthly caps on transcription, per person (default 60) and overall (default 1500) |
| `MEDIA_ENABLED_AT`, `MEDIA_BACKFILL_DAYS` | From when channel voice notes and images are recorded (unset: never), and how many days before that also count for messages ingest writes from now on (default 0) |
| `TRACING_ENABLED`, `LANGFUSE_HOST` | Export runs for study. Off by default |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Credentials for that destination |
| `LANGFUSE_ENVIRONMENT` | Langfuse environment to export to and search on opt-out (`production`) |
| `TRACE_RETENTION_DAYS` | Days an exported trace is kept; `ingest` deletes this app's older traces daily (default 90). `/privacy`, the one-time tracing notice and the capabilities reply state the same period |
| `BACKUP_RETENTION_DAYS` | Days a database backup is kept, as configured where backups run. Only stated, by `/privacy`; unset (the default) says no backups are kept, so set it when backups are turned on |

Anything that reaches outside the server is off by default. A deployment should
acquire an outbound boundary because somebody chose it.

## Working on it

This project is spec-driven. Read `openspec/changes/*/proposal.md` before
changing behaviour, and `design.md` for why things are shaped as they are.

```bash
just specs            # active changes and their progress
just spec             # validate every spec strictly
```

Further reading:

- [docs/discord-setup.md](docs/discord-setup.md) — preparing Discord
- [docs/deploy-coolify.md](docs/deploy-coolify.md) — deployment runbook
- [docs/admin-console.md](docs/admin-console.md) — the operator console
- [docs/operations.md](docs/operations.md) — running it day to day
- [docs/mcp-interface.md](docs/mcp-interface.md) — the MCP interface
- [docs/document-ingestion.md](docs/document-ingestion.md) — attachments and links
- [docs/roadmap.md](docs/roadmap.md) — what has shipped, what is in progress, and what is next

## Two things to know before running this anywhere real

**It creates a permanent searchable archive of everything said in indexed
channels.** That needs a retention window, disclosure to the team, and an
opt-out. All three exist; the policy is a decision, not a default.

**Tracing copies questions and answers into a store with no permission rules.**
With `TRACING_ENABLED` on, each question and the answer sent are exported to
Langfuse, which has no notion of who may read a channel. Evidence leaves as
references only (window, channel, source, score), never its text, but an answer
can still paraphrase what it drew on. Deleting a message does follow — the
tombstone deletes the traces built from it, and a failed deletion is retried —
but Langfuse logins are limited to the console admins and the destination has
to be protected the way the database is. It is off by default for this reason,
and when it is on each person is told once, on their first traced reply.

**No bot can read direct messages between people, on any platform.** Questions
like "what did people ask me today" cover indexed channels and DMs sent to the
bot, and nothing else.
