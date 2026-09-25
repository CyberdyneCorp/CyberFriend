# Roadmap

What CyberFriend does on `main` today, what is being built, and what is next.
PR numbers are the merged pull requests on GitHub (the earliest work predates
them and has none); the specs for each item are
under `openspec/changes/`.

Last reviewed 2026-09-25.

## Shipped

### Channels and memory

- **Answers from indexed channels, with citations** — hybrid lexical and vector
  retrieval scoped in SQL to what the asker may read; each claim links back to
  its message. Built before the repository used pull requests; later
  capability routing in #25 and #40.
- **Audience-aware answers** — in a channel only what everyone there can read is
  cited; a DM gives the asker's full view, and withheld evidence is noted to the
  asker alone.
- **Catch-up and digests** — *o que eu perdi no #general?*: a cited digest of one
  channel for a period, in sections (decisions, open questions, requests,
  highlights), in the asker's language; typed `#name`s resolve; schedulable as a
  daily digest (#35, #66).
- **What someone said** — *o que o João disse sobre o deploy semana passada?*,
  including *falou com você* / *told you*, over that person's own messages and
  calendar days in `ANSWER_TIMEZONE`; people archived under an account id are
  named when ingest starts (#72, #73, #74).
- **Conversation memory** — follow-ups keep context per person and place; chain
  and market turns are remembered; `/forget` here or everywhere (#29, #53, #62).
- **Index from chat and see what is archived** — `/index`, `/unindex`,
  `/channels` (#30, #44).
- **Documents** — attachments and linked documents, parsed in a sandboxed child
  process.
- **Channel media recording** — from `MEDIA_ENABLED_AT`, voice notes and images
  in indexed channels are recorded as pending rows; nothing is downloaded yet
  (#81).

### Obligations and decisions

- **Obligations** — what people asked of each other, extracted as messages
  arrive; `what do I need to do?`; closed with a ✅ reaction or `/resolve`; DM
  notifications, switched with `/notifications` (#35).
- **Decision log** — decisions extracted in the same pass (#76, re-landed as
  #77), answered from the log with no model call, dated and cited (#78), and a
  `decisions-backfill` command for history captured before the feature (#79).

### Personal facts

- **Facts you tell it yourself** — full and preferred name, email, phone, home
  address, birth date, preferred language, and up to five Ethereum and five
  Bitcoin wallets; several facts in one introduction; contact details and wallets
  shown only in a DM (#32, #48, #52, #68).
- **Your language** — answers in the language asked in, and fixed replies
  localised to Portuguese or English (#43, #54, #63).
- **Knows the date and time** (#48, #49).

### Crypto

- **Wallet balances** on Ethereum, Base and Arbitrum, with USD values (#39, #41,
  #42).
- **DeFi positions** — Uniswap v3/v4 liquidity (range, fees, in or out of range)
  and Aave v3 supplies, borrows and health factor (#50, #51, #53, #67).
- **Portfolio total** per chain and wallet (#61).
- **Wallet activity** — swaps, liquidity, Aave, transfers and gas; address-poisoning
  spam hidden and flagged; a warning when an explorer is behind (#69, #71).
- **Alerts** — LP out of range or near its edge, Aave health factor, BTC/ETH
  price; created by asking in words and pressing Confirm; `/alert list`,
  `/alert delete` (#59, #60, #65).
- **Market data** — BTC, ETH, the S&P 500 and currency conversion (#28, #30).

### Voice

- **Voice questions in DMs** — a voice message sent to the bot is transcribed
  and answered as if typed, with hard monthly caps; off by default. Confirmed
  working in production on 2026-09-25 (#80).

### Scheduling and operations

- **Scheduled questions** — `/schedule create`, `list`, `delete`; a DM only when
  there is an answer (#45, #46).
- **Admin console** — federation, channels, retention, opt-outs and tokens (#22,
  #23, #24).
- **MCP interface** to the corpus, under the same permission rules; its
  permissions made authoritative after a gateway RESUME in #37.
- **Langfuse tracing**, off by default, with deletions following (#36, #38).
- **A progress note** for slow answers (#64).
- **End-to-end harness** — the real bot process over a fake Discord wire and a
  real database, including the ingest ask-extraction path (#56, #57, #58, #75).
- **Slack and WhatsApp specs** — OpenSpec only, no implementation (#70).

## In progress

- **Preferred currency** — a personal fact that shows dollar figures in the
  asker's own currency beside USD. PR pending; not on `main`.

## Planned

- **Transcribe channel voice notes into search** (media PR 9) — off by default,
  a 1,500-minute monthly cap shared with voice questions, and a per-person media
  opt-out (`openspec/changes/add-media-transcription`).
- **Describe and read screenshots** (media PR 10) — off by default, 3,000 images
  a month.
- **Aerodrome on Base** — Slipstream concentrated liquidity (held and staked) and
  classic vAMM/sAMM pools.
- **Routing unification**, PR4 to PR12 — paused.
- **More obligation phrasings** — *o que me pediram esta semana?*, *what was I
  asked?*.
- **CyberWealth finance over MCP**, once CyberWealth ships its MCP service —
  a per-server Bearer (`FEDERATION_AUTH_<NAME>`), a person's `cwk_` key held as a
  DM-only encrypted secret, and `my_*` tools answered only in a DM.

## Waiting on decisions

- **Slack** — an internal app or a Marketplace listing
  (`openspec/changes/add-slack-platform`).
- **WhatsApp** — Meta's business policy bans general-purpose AI assistants
  (`openspec/changes/add-whatsapp-platform`).
