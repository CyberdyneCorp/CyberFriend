# Operations — retention, opt-out, and CI

CyberFriend builds a searchable index of everything said in the channels you
point it at. That is the feature. It is also the reason two controls on this
page are a precondition for running it anywhere real, rather than nice-to-haves:
without them the bot is a permanent, queryable archive of your team's
conversations that nobody consented to and nobody can leave.

## Retention

`RetentionService` purges messages, the windows built over them, extracted asks,
extracted decisions, the document fetch log, and document entries older than a
configured window. A decision also goes when any message it rests on (the
proposal it settled, shown to the model as context) is older than the cutoff,
because its summary may restate that message.

Deleting a message in Discord works the same way: ingest withdraws every
decision it stated or rests on as evidence at once, without waiting for
retention, since the summary may restate the retracted words.

```python
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.adapters.documents.store import PostgresDocumentStore
from chatmemory.app.retention import RetentionPolicy, RetentionService

service = RetentionService(
    corpus=PostgresRetentionStore(engine),
    policy=RetentionPolicy.from_days(180),
    documents=PostgresDocumentStore(engine),
)
report = await service.run_once()
```

**The default is to retain indefinitely.** `RetentionPolicy.from_days(None)`
purges nothing and says so in the log. That is deliberate: a default that
silently deleted a team's history on first deploy would be the worse of the two
failures. Turning retention on is a decision somebody makes.

**Pass `documents=` or the pass is not a purge.** An upload outlives the message
that carried it, in its own tables. A `RetentionService` built without a
document store logs `retention.documents_not_covered` on every pass, because a
report that says "12 400 rows purged" while every PDF the team ever attached
stays indexed reads exactly like compliance.

### What "complete" means here, and why the order is what it is

A window is one block of text formed from its messages, and search reads that
text — it never re-derives it from the messages. So deleting a message and
leaving its window behind removes the row and keeps the content, fully
searchable. Windows are therefore deleted first, and by `starts_at`: a window
that straddles the cutoff carries pre-cutoff text however recently it ends.
Anything left holding no messages at all is swept afterwards.

Every statement is a delete keyed on a timestamp that does not change, so the
pass is re-runnable: one that fails halfway leaves a smaller corpus, and the
next pass finishes the job. Nothing records progress, so there is no progress to
get out of step.

Deleting a window takes its surviving neighbours out of retrieval until they are
re-formed. The pass marks the affected channels dirty so the next window rebuild
does that, rather than leaving it to the "message in no live window" safety net.

## Per-person opt-out

```python
from chatmemory.app.optout import OptOutService

service = OptOutService(
    registry=PostgresRetentionStore(engine),
    documents=PostgresDocumentStore(engine),
)
await service.opt_out(PersonRef("discord", 123456789), reason="requested 2026-09-13")
```

This removes their messages, the windows those messages appear in, the asks
extracted from and addressed to them, the decisions they stated or whose
evidence they wrote (a decision somebody else stated in reply to their
proposal may restate it), their reactions, the mention index rows pointing at
them — **and the documents they uploaded**. An opt-out that covers
messages and leaves the attached PDF searchable has withdrawn the index entry
and kept the content, which is the wrong half.

**It survives re-ingestion.** Discord still holds their messages, and backfill
re-reads history from Discord. The exclusion is therefore enforced by a database
trigger (migration 0008), not by the ingestion service: a row whose author has
opted out is silently dropped before it is stored, whatever issued the INSERT.
Nothing that is added later has to remember to call anything.

The trigger *skips* the row rather than raising. An exception would abort the
whole backfill page and stop ingestion for everybody else in the channel.

**Opting back in restores nothing.** `opt_in` clears the exclusion so future
messages are captured again. What was purged is gone; only what Discord still
holds and a later backfill re-reads comes back.

### The boundary you will have to explain

Other people's messages that *mention* the opted-out person still exist, and
still contain their name. Removing those would mean deleting other people's
words on one person's request. What the opt-out does remove is the mention
index — the person-keyed path into those messages — so no query keyed on them
finds them. If somebody needs the text itself gone, that is a message deletion
in Discord, which the bot already follows.

Asks *addressed to* the person are deleted, unlike mentions. An obligation
report that still lists "Alice, can you review this" has not withdrawn Alice
from anything.

Voice questions are refused for an opted-out person: nothing is downloaded and
nothing is sent to the transcription endpoint. Their `media_usage` rows (seconds
per month, no content) are kept, because dropping them would hand the month's
minutes back to the deployment's ceiling.

## Running the migration

Migration `0008` creates `person_opt_out` and the two triggers. It chains onto
`0007`; `alembic upgrade head` applies it in the usual way. Nothing outside the
migration creates that table — see `tests/unit/test_retention_migration.py`,
which asserts the trigger bodies still say what the code assumes.

## CI

`.github/workflows/ci.yml` runs on every pull request and every push to `main`:

1. `openspec validate --all --strict` (openspec installed from npm)
2. `ruff check src/ tests/`
3. `mypy`
4. `pytest tests/unit` — before the database is touched, so a broken service
   container cannot hide a failing unit suite
5. `alembic upgrade head`, then `pytest tests` without `tests/e2e`
6. `pytest tests/e2e` with `E2E_REQUIRE_DB=1`, printing the ten slowest tests

A pgvector Postgres runs as a service container, because the integration tests
are where the permission predicate is proved against a real planner and a real
approximate index — and those tests *skip* rather than fail when no database is
reachable. A CI job without one is green and proves nothing.

No OpenAI key is supplied. The tests that would spend money skip themselves, and
a pull request from a fork must not be able to bill you.

## End-to-end tests

`tests/e2e` drives the bot process `main` runs -- built by the same
`assemble(settings, edges)` -- with fakes only at its edges: the Discord wire
(real discord.py objects fed gateway-shaped payloads, with REST and webhook
calls recorded), a scripted chat model and hashed embeddings, and one
`httpx.MockTransport` for every outbound provider. The database is real:
`TEST_DATABASE_URL` (the compose pgvector, after `just migrate`), or a
throwaway testcontainers pgvector when that is unreachable. Any other HTTP
connection, or a request to a host no fixture answers for, fails the turn that
made it -- also when the provider that made it caught the error and answered
"could not be reached". The corpus is seeded through the production store and
embedding worker, so its windows carry message ids as ingest leaves them.

The ingest half is there too, for asks: `bot.chatter(...)` is a channel message
not addressed to the bot, captured by the ingest entrypoint's `live_loop`, and
`bot.extract_asks()` flushes the extraction worker `build_ask_pipeline`
assembles. Only the extractor is scripted -- it sends the production prompt to
the scripted model, which answers with what `bot.chat.script_asks(...)` and
`bot.chat.script_decisions(...)` set for that message -- so a scenario runs
capture, extraction, question and cited answer on the real ask tables, and
`bot.decision_rows()` reads what the same pass stored as decisions.

Voice questions use the same wire: `dm.say_voice(attachment_payload(url, ...))`
sends a DM carrying an attachment and the `IS_VOICE_MESSAGE` flag. Neither the
Discord CDN nor the transcription host is a default fixture, so a scenario
scripts them (`serve_bytes`, `ScriptedTranscription`) and any other scenario
reaching one fails.

Scenarios assert only what an outsider could see: what Discord received,
whether the corpus was searched, which hosts were reached, which model stages
ran, and the memory and fact rows, read by SQL. Every `/name` the bot says is
checked against the commands Discord offers where it said it.

- `E2E_REQUIRE_DB=1` turns "no database" from a skip into a failure. CI sets it.
- The suite fails the run if it takes more than 90 seconds.
- `E2E_UPDATE_SNAPSHOTS=1` rewrites `tests/e2e/snapshots/commands.json`, the
  exact command payload the bot syncs; review the diff like code.
- discord.py is pinned to `~=2.7.1` in the dev extras because the fake wire
  uses a few of its private names; `tests/e2e/test_harness_canary.py` lists
  them and fails first on an upgrade that moves one.
- A known-open defect is a strict `xfail` whose reason names the constant.
  Fixing it turns the xfail into a failure, so the marker comes off with the fix.
  It expects `LanguageMismatch` only, and first checks the reply sent is that
  constant, so a harness failure or a changed reply is not absorbed by it.
- `mypy` checks `tests/e2e` as well as the package, so the harness's
  `type: ignore`s against discord.py are verified, not assumed.

A fix for a production bug adds a scenario here that replays the transcript
that failed -- the message, and the reply that was wrong -- as well as a unit
test of the predicate that caused it. A unit test of the predicate alone is how
each of those bugs passed review.

## The SQL audit

`tests/unit/test_sql_audit.py` scans the store's SQL modules for every statement
and requires each one to either bind `:channel_ids` — the viewer's readable
channel set — or carry a written reason for running without a viewer.

This replaced a hand-maintained list, which audited the statements somebody had
remembered to add to it. Three content-returning SELECTs were added outside it
and nothing failed. If you add a statement and CI tells you about it, the fix is
to write down why it may run unscoped, in one sentence, in `UNSCOPED`. "It is a
maintenance query" is not a reason; "ingest-only, unreachable from any
request-scoped surface" is, and is checkable.

## Known gaps

These are real and are not fixed by anything on this page:

- **Nothing schedules retention.** There is no `RETENTION_DAYS` setting in
  `chatmemory.config.Settings` and no loop in `entrypoints/ingest.py` that calls
  `run_once`. Until both exist, retention runs only when an operator runs it.
- **Nothing exposes opt-out to the people it is for.** There is no Discord
  command and no MCP tool; an opt-out today is an operator acting on
  somebody's behalf, from the admin console (`/api/optouts`) or by running
  `OptOutService.opt_out`. A consent control that requires filing a ticket is a
  weak one.
- **`OptOutService.filter_messages` is not called by ingestion.** It is an
  optimisation, not the guarantee — the trigger is the guarantee — but until it
  is wired into `IngestService`, every excluded message costs a round trip to
  the database to be rejected.

## Console sign-in secrets

When CyberdyneAuth sign-in is configured (see
[admin-console.md](admin-console.md#signing-in-with-cyberdyneauth)), the
`admin` service holds two secrets besides `DATABASE_URL`. Neither reaches the
corpus or the bot's accounts, and neither is ever logged, recorded in
`config_audit` or shown by the console.

| Secret | What it is | If it leaks |
|---|---|---|
| `ADMIN_OIDC_CLIENT_SECRET` | The `cyberfriend` client's secret at CyberdyneAuth. | Rotate it at CyberdyneAuth and redeploy `admin`. |
| `ADMIN_SESSION_KEY` | 32 bytes (base64) that encrypt the tokens in `admin_session`. `openssl rand -base64 32`. | Replace it and redeploy: every existing session stops decrypting and people sign in again. |

The other sign-in variables (`ADMIN_OIDC_ISSUER`, `ADMIN_OIDC_CLIENT_ID`,
`ADMIN_PUBLIC_URL`) are not secret. Set all five or none: a partial set refuses
to start and names what is missing.

**Break-glass.** If CyberdyneAuth is down while an admin change is urgent,
unset `ADMIN_OIDC_ISSUER` (and the other four) and redeploy `admin`: sessions
stop being accepted and `cfa_` tokens are admin again. Set them back once
CyberdyneAuth is up. While CyberdyneAuth is merely unreachable, existing
sessions keep working until their access token needs a refresh, and bearer
tokens keep working throughout.

`admin_session` and `admin_login` hold hashes and ciphertext only. Revoked and
expired sessions stay until the table is pruned; used and expired logins are
dropped a day after they expire, when the next sign-in starts.

## Tracing

With `TRACING_ENABLED=true` and a Langfuse destination configured, every run is
exported: the question as asked, the answer as sent, the decision trail, and
each piece of retrieved evidence with its text. This is what makes it possible
to say later whether the assistant is getting better.

| | |
|---|---|
| `TRACING_ENABLED` | Off by default. Both `bot` and `ingest` need it |
| `LANGFUSE_HOST` | e.g. `https://langfuse.example.com`, no trailing path |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | The project's API keys |
| `TRACING_TIMEOUT_SECONDS` | What one export may cost before it is abandoned |

Turning it on with a host but no keys is refused at startup rather than
silently disabled. A deployment that believes it is recording and is not finds
out on the day somebody asks what went wrong.

### What this means for confidentiality

The trace store holds verbatim content from every channel the assistant has
retrieved from, and it has no viewer scoping — none of the rules that decide
who may read what in Discord apply to it. **Treat access to Langfuse as
equivalent to access to the database.**

Two things limit the exposure, and it is worth knowing exactly what they do:

- **Deletion follows.** Deleting a message in Discord deletes every exported
  trace that quoted it. The corpus tombstone is applied first and never waits
  on the trace store, so a destination that is down delays the withdrawal
  without delaying the deletion. Unconfirmed withdrawals are retried by a sweep
  in the `ingest` process every five minutes.
- **An opt-out is honoured.** Nothing is exported for a person who has opted
  out of indexing. If the opt-out registry cannot be read, the run is withheld
  rather than exported.

Neither of these makes the destination safe to share widely. They keep the
project's deletion and opt-out guarantees true across the copy; they do not
give the copy permissions of its own.

### Checking it is working

```bash
curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
  "$LANGFUSE_HOST/api/public/projects"
```

The bot logs `composition.tracing enabled=true` at startup when a destination
is configured, and `reasoning.trace_failed` when an export is dropped.

## Wallet balances

With `WALLET_TOOLS_ENABLED=true` and an `INFURA_KEY`, the assistant can report
what a `0x` address holds on Ethereum, Base and Arbitrum: the native ETH balance
and a named set of ERC-20 tokens, each with its USD value from the same price
source the market tools use. One key covers every chain; the key must have
Arbitrum enabled in the Infura dashboard. It also enables DeFi positions, below.

### Only an address the asker typed

The lookup is held to the egress guard's **rooting** rule, not to a closed
vocabulary — addresses have no fixed set to be a member of. Rooting means an
argument survives only if it appears in the asking person's own question, so:

- an address **you type** is looked up;
- an address the assistant found in a channel, document or tool result is
  refused.

That is the property that keeps this from becoming a way to sweep every address
mentioned across the channels the bot can read, which is more channels than most
individuals can see. A rooted word that is not a well-formed address is refused
outright rather than trimmed — trimming an address to whichever part of it was
valid hex produces a different, valid-looking address belonging to someone else.

### It is decided before retrieval

A wallet question never reaches the corpus. A balance is not in it and cannot
be: a channel message about a wallet is a record of what somebody said, and
answering from one reports a colleague's project summary as somebody's balance
— which is exactly what happened before this route existed.

An address is still allowed to be what a question is *about*: "what did people
say about 0x…" keeps its corpus answer, because the conversation verbs mark it
as a question about the conversation. Asking about a wallet without naming an
address is answered by asking for one, not by searching. A bare "what's my
balance?" / "qual o meu saldo?" reads the asker's saved wallet; a question asking
for a *total* ("what's my wallet's total balance") is the portfolio's, below.

### What it cannot see

JSON-RPC cannot enumerate holdings. `eth_getBalance` gives the native balance;
every token requires knowing a contract to call `balanceOf` on. So the token set
is **named, not discovered** — currently USDC, USDT, DAI and WETH on Ethereum
and Arbitrum, and USDC, DAI and WETH on Base. A token nobody listed is invisible to the
lookup rather than absent, and the answer says so. Adding one is a line in
`adapters/chain/tokens.py`.

Full discovery would need a provider with an indexed view (Alchemy's
`alchemy_getTokenBalances`, or Etherscan's Pro endpoint) rather than a node.

### It can only read

`eth_getBalance` and `eth_call` are the entire surface. There is no signer, no
private key and no mnemonic in `adapters/chain`, so nothing there could sign a
transaction even if later code asked it to — the allowlist entry declares
`READ_ONLY` and the package agrees with the declaration.

A chain that cannot be reached is reported as unreachable, never as an address
holding nothing; one endpoint failing still reports the other. A rate-limited
balance read is retried with the same back-off as positions reads (up to 7.5 s)
before a chain is called unreachable.

## DeFi positions

The same switch and key also register the `defi_positions` server, with three
positions tools, the portfolio total and wallet activity (below), all read-only. The route picks
exactly one from the question, so the model's only job is to copy the address:

| Asked about | Tool | Reports |
|---|---|---|
| pools, liquidity, LP, Uniswap, ranges | `liquidity_positions` | Every open Uniswap v3 and v4 position: pair and fee tier, amounts and USD value, 🟢 in / 🔴 out of range, min/max price and current price, uncollected fees. Only open positions: withdrawn NFTs are left out entirely |
| Aave, borrow, supplied, collateral, health factor | `lending_positions` | Aave v3 (main market) supplied and borrowed assets with USD values and APYs, total collateral and debt, health factor |
| both, or "defi positions" | `defi_positions` | Both reports |

It uses the same address rules as balances: the address must be in the question
or be the asker's saved wallet, and a question with neither is answered by
asking for one. A question needs an address or a first-person reference ("my
pools") to route here — "what did we decide about the pool" is a decision
question. A follow-up such as "show me the v4 position" uses the address from
the asker's own last three questions, if one named an address.

### How positions are found

- **Uniswap v3** positions are listed by the chain (`tokenOfOwnerByIndex`).
  Uncollected fees come from *simulating* `collect` with `eth_call` as the
  owner; nothing is sent.
- **Uniswap v4**'s position manager cannot list an owner's tokens, and Infura
  limits `eth_getLogs` to 10,000 blocks. The token IDs come from Blockscout's
  public API (no key), and each is then confirmed with `ownerOf` on-chain. The
  explorer is trusted for nothing else. It also lags, and on Arbitrum it
  did not index a v4 position at all: when the chain reports more v4
  positions than it returns, the owner's `balanceOf` is binary-searched over
  archive state for the block each missing position arrived in (about 30
  `eth_call`s), that one block's `Transfer` log names it, and it is confirmed
  with `ownerOf` too. There is no age limit. Only what neither finds is reported as
  "could not be listed".
- **Aave v3** contracts are resolved from each chain's addresses provider, so
  an Aave upgrade is followed automatically.

Reads are batched through Multicall3 (50 calls per request), and a
rate-limited request is retried with back-off (up to 7.5 s). Chains are read one after another rather than
concurrently, because concurrent reads tripped Infura's rate limit in
production. A wallet with 134 position NFTs takes a few seconds per chain.

### Values

USD values come from the Aave oracle on the same chain, so pricing adds no
outside service. A token the oracle does not price is valued from the pool's own
price when the other side is priced, and the answer says so — for a thin pool
that mark can be far from what the tokens would sell for. Aave balances worth
under a cent are counted rather than listed.

`POSITIONS_TIMEOUT_SECONDS` (default 25) bounds each chain; the combined tool
reads liquidity then lending, so its server timeout is twice that.

### Not covered

Other exchanges (Aerodrome, SushiSwap, …), Aave's other markets (Prime, EtherFi),
P&L and impermanent loss. History is the activity tool's (below). Adding a chain is one entry in
`adapters/chain/deployments.py`.

## Portfolio total

A fourth tool on the same server, `portfolio_summary`, answers "quanto eu tenho
no total?", "what's my portfolio worth?", "what's my net worth on chain?" and
"e no total?" after a wallet or positions question. It is recognised before
retrieval (route label `PORTFOLIO`, ahead of the positions and balance routes)
and needs no setting beyond the positions ones.

| Per chain | What is counted |
|---|---|
| Wallet | Native ETH, the named tokens, and every asset the chain's Aave v3 market lists (which is how cbBTC is found). Underlying tokens only: an aToken or debt token is never read as a balance |
| Liquidity | Open Uniswap v3/v4 positions, **including uncollected fees** (shown on the line) |
| Aave | Per asset, (supplied − borrowed) × oracle price, so a supply not enabled as collateral still counts. The health factor is shown |

The answer is a line per chain (largest first; chains holding nothing share
one line), a subtotal per wallet when there are several, and a grand total in
USD, in the question's language, followed by what is not included: other
tokens, other exchanges and protocols, other Aave markets, and anything held
that has no price.

**Whose wallets.** The asker's saved wallet, plus any address they typed about
their own money ("my portfolio with 0x…"), at most three per question. A typed
address in a question about that address ("what is 0x… worth in total?") is
read alone. The tool's `addresses` argument is cleared piece by piece like the
single-address tools: each rooted in the question or the saved wallet, the
whole call refused if any piece is not an address. No address is written out
in full, citation included: the answer can be given in a channel, so each
wallet is named by its last four characters.

**Prices.** Each chain's Aave oracle, for ether (as WETH) and every reserve; a
dollar stablecoin the oracle does not list is $1; if the oracle does not
answer, ether is priced from CoinGecko and the answer says so.

**Partial answers.** Each section of each chain is tried twice. One still
unread makes the headline "**At least $X** — not read: Base (Aave)", never a
plain total. Everything shares one deadline of three sections × chains ×
`POSITIONS_TIMEOUT_SECONDS`, which is also the server's timeout.

**Cost.** One node per chain for the whole lookup, however many wallets: Aave's
contracts are resolved once, and the reserve list with its symbols is cached
per process for an hour (it is public and the same for everyone). A wallet is
about one and a half times the reads of the combined positions tool, and
takes as long as that tool on the same wallet (seconds, or ~30 s for a v4
position the explorer has not indexed on Arbitrum).

## Wallet activity

A fifth tool on the same server, `wallet_activity`, answers "o que essa
carteira fez essa semana?", "what did my wallet do this week?", "minhas
transações de ontem", "show my wallet activity" and "what did it do?" after a
wallet question. It is recognised before retrieval (route label
`WALLET_ACTIVITY`, ahead of every other chain label) and needs no setting
beyond the positions ones. "What did people say about my wallet" stays with
the corpus.

**Whose wallet.** An address in the question, one carried from the asker's own
earlier question, or the saved wallet; with several saved and none named by
its last characters, the reply asks which. Cleared like the positions tools.

**The window** is read from the asker's question as the clearance carries it,
never from the model's arguments: "hoje", "ontem", "essa semana", "semana
passada", "este mês", "nos últimos N dias", "today", "this week", "last N
days"… Seven days when none is named, at most 30 (a wider one is cut, and the
answer says so). The period is always stated, in UTC.

**Where it reads.** Per chain, Blockscout's `/api/v2/advanced-filters` for the
wallet (top-level, internal and token transfers, time-filtered on the server),
the three explorers in parallel; then, one chain after another, Aave's reserve
list with each reserve's aToken and debt token (cached per process for an
hour) and the oracle's current prices. Not `/addresses/{a}/transactions`: an
EIP-7702 wallet's relayed actions are missing from it. Blockscout's cursor
sends null fields as empty strings (left out, the server loops on page one);
at most four pages (200 rows) per chain, and more is stated. One extra page of
the address's own transactions is read only to name a Uniswap multicall's inner
calls.

| Kind | How it is recognised |
|---|---|
| Uniswap | A position NFT moved, or the v3/v4 position manager was called or paid: opened, added, removed, collected fees, or "LP withdrawal (liquidity and/or fees)" when relayed |
| Aave | An aToken or debt token moved: supplied, withdrew, borrowed, repaid, in the underlying (an aToken mint includes interest) |
| Swap | One asset out and a different one in, native ETH included |
| Sent / received | Everything else, with the counterparty. A payment to a plain address inside a larger transaction (a relayer's cut) is its own line and is never called a fee |
| Other | A transaction the wallet sent that moved nothing (an approval), by its method |

Gas is what the wallet paid on transactions it sent; relayer-submitted ones are
counted separately. USD is at current prices, and says so.

**Spam and poisoning.** Hidden and counted, never by Blockscout's reputation
(it rated every poisoned token "ok"): tokens outside the named set and the Aave
assets in transactions the wallet did not send, zero-value transfers, and
third-party deposits under $0.01. A hidden transfer whose counterparty shares
the first and last four characters with a real one adds a ⚠️ address-poisoning
warning.

**Privacy.** The egress clearance carries whether only the asker reads the
answer (`private`, from the question's audience; false unless set). In a DM
every address is printed in full and never shortened, because a shortened
address is what poisoning imitates. In a channel a counterparty is "an external
address" / "um endereço externo" and the wallet is `…` plus its last four
characters, citation included.

**Failures.** An explorer that fails or times out is "could not be read",
never "no activity"; without Aave's tokens the answer says Aave actions may be
missing. Each chain is bounded by `POSITIONS_TIMEOUT_SECONDS`.

## Seeing what is archived

`/channels` lists the archived channels the person asking can read. Private,
and deliberately not a directory.

The listing is **indexed scope intersected with the asker's readable
channels**, both read per request: scope changes without a redeploy, and access
changes without anything happening here at all. It resolves permissions through
the same `AclResolver` that scopes retrieval, so the list cannot disagree with
what they can actually search — a listing built from a second permission check
could name a channel that returns nothing, or omit one that returns something.

**Nothing is said about what the intersection removed** — not the names, not
the count, not that there were any. A count is a disclosure: "and 4 you cannot
read" tells somebody four private archived channels exist, which is most of
what the ACL design exists to withhold. The reply for "you can read none of
them" is identical whether the server archives nothing at all or archives only
channels this person cannot see.

Operators who need the full picture, with message counts, use the admin
console's channel view rather than this command.

## What someone said

"o que o João disse sobre o deploy semana passada?", "what did Ana say about
pricing yesterday?", "o que eu falei sobre X?", "@Maria comentou algo sobre Y
ontem?". Recognised without a model call (`app/said_by.py`), after catch-up
and before the ordinary answer; obligation questions ("o que o João me
pediu"), market questions, fact requests and compound questions keep their
own routes. Provenance is logged as `ask.said_by` with `route=SAID_BY` and
`outcome` one of `resolved`, `ambiguous`, `fallback`, `unavailable`.

**It is bounded by the asker's own access.** Filtering a colleague's
messages only ever covers what the person asking could already read in
Discord, narrowed further by the room the answer lands in. The author, the
span, the viewer's channels and tombstones are all one statement
(`sql.AUTHOR_SEARCH`); the person filter can only remove rows.

**Who.** A mention is used as is; "eu"/"I" is the asker; a name is matched
among people with a live message in a channel the asker and the room can
read (`SearchBackend.people_named`, `sql.PEOPLE_VISIBLE`), accent- and
case-insensitive, exact full name first, then first name, prefix or surname.
Several matches get "Qual João? …" listing at most six — no search, no model
call, and nobody who only speaks in channels the room cannot read. No match
at all falls through to the ordinary answer, so "what did the docs say" works
as before. A typed "@Maria" (not picked from autocomplete) is a name like any
other. Two people with the same display name are listed once and the reply
asks for a mention, the only thing that tells them apart. Per-server
nicknames are not stored; mention such a person.

**When.** `app/timespan.py` in `ANSWER_TIMEZONE`: "semana passada" is the
previous Monday-to-Monday, "ontem" the local day. A question naming two spans
or a range falls through rather than searching one end of it, and so does one
naming a day no rule reads ("de segunda a quarta", "1 a 5 de setembro", "on
monday") rather than searching all of the person's history. A month alone
("o evento de setembro") is part of the topic. Catch-up and
obligations still use their older rolling, UTC-cut periods.

**How much is read.** The topic is ranked over all of the person's messages
in the span, and the best 200 (`AUTHOR_CANDIDATES`) are grouped into at most
20 conversations; an old on-topic message is not lost behind newer chatter.
With no topic, the newest 200 are used.

**What the model sees.** Only the person's own lines from each conversation,
each stamped in UTC, and the citation lands on their own message. Messages
not yet windowed by ingest (seconds old) are not found yet. The answer's
first line states who and when, so a wrong resolution or span is visible.

**One empty reply.** Nothing said, said only in private channels, opted out
or deleted all read "I found nothing X said … in the channels I can search
here", in the question's language. A retrieval failure is reported as a
failure, never as silence. No withheld-evidence notice is sent for this
route.

**Cost.** One topic embedding and one synthesis call per resolved question
(none without a topic for the embedding); the ambiguity and fall-through
replies cost no model call. Migration `0023` adds `ix_message_author_time`,
a partial index on `message(author_person_id, created_at)` that serves both
statements.

## What we decided

"o que decidimos sobre o deploy?", "o que ficou decidido semana passada?",
"qual foi a decisão sobre o banco?", "what did we decide about pricing?",
"what was decided last week?". Decisions are read out of conversation by the
ask extraction pass (one model call per candidate message, both arrays in
one reply) and stored in the `decision` table (migration `0024`); this is the
read side.

**Recognised without a model call** (`routing.decision_question`), in the
answer chain after obligations and before the ordinary answer. Only what a
group settled: "we", "a gente", the passive. "What did you decide" (asks the
bot), "o que o João decidiu" (one person), "decide between A and B" (asks
for help choosing), a pronoun topic ("sobre isso"), compound questions and
market or fact questions keep their own routes.

**When.** `app/timespan.py` in `ANSWER_TIMEZONE`, as for said-by: calendar
days and Monday-to-Monday weeks. A range or an unreadable day falls through.
A question with no topic lists the period's decisions; with neither topic nor
period, the last 30 days, and the heading says so.

**Scope.** The asker intersected with the room (`retrieval_viewer`), bound
into `decisions_sql.SEARCH_DECISIONS` itself: the decision's channel, the
source message alive, and every evidence message (the source plus what the
model was shown with it) existing, alive and in a channel the viewer may
read. A deletion also withdraws the rows at ingest; the read predicate covers
one that lands in between.

**Ranking.** An exact scan: `0.7 * cosine(topic embedding) + 0.3 * ts_rank`
over the `'simple'` tsvector, confidence at or above the policy's 0.7. A row
is listed only if its cosine reaches `DECISION_MIN_SIMILARITY` (default 0.4)
or, for a row stored without an embedding, all of the topic's meaningful
words occur in it. The best five are listed newest first, each dated in
local time and citing the message that settled it with its original words.
Decisions are not merged or superseded; the dates show the order.

**No "nothing was decided".** Nothing above the floor, no readable channel,
or a failed topic embedding: the question is answered by ordinary retrieval,
which has its own single "found nothing" reply. A missed extraction reads as
a retrieval answer, never as a claim that nothing was settled.

**Cost.** One topic embedding (none without a topic), no chat-model call.
Logged as `decisions.looked_up` with `found` and `model_calls=0`.

### Backfilling history captured before the decision log

History the ask pass read before decisions were extracted is recorded as
extracted (`message.asks_extracted_seq` equals `asks_extraction_seq`), so the
backlog worker never reads it again and the decisions in it cannot be
answered. To put it back in the queue:

```
just decisions-backfill --since 2026-06-01 --until 2026-09-01
```

(`python -m chatmemory.entrypoints.decisions_backfill --since YYYY-MM-DD
[--until YYYY-MM-DD]` in a container.) It resets `asks_extracted_seq` to NULL
only for messages that are

- live (not deleted),
- created on or after `--since` and, with `--until`, before it, both from
  midnight UTC,
- in a channel in indexing scope, read as ingest reads it (stored
  configuration, then the environment; if stored configuration cannot be read
  the command stops and resets nothing),
- matching `DECISION_MARKERS`, the candidate filter's own pattern, matched in
  Python so the command resets exactly what the filter will send to the model.

It prints how many it reset, how many matched but were already pending, and
how many it scanned, then exits. Nothing is extracted by the command: the
ingest process's `BacklogExtractionWorker` drains the reset messages at its
usual rate bound (100 messages a pass), and `pending` on the ingest health
endpoint shows it draining. Running it twice is harmless; the second run
resets nothing still pending.

**Cost.** One extraction call per reset message, on `EXTRACTION_MODEL`,
metered in the usual ask usage. Pick the window deliberately. The reset does
not tell history read before the decision log shipped from history read
after it: every already-extracted marker-bearing message in the window is
reset, and one the pass has already read for decisions is paid for again for
nothing. Pass `--until` the day decision extraction was deployed; without it
the window runs to now.

**Warning: asks on those messages are re-extracted too.** The model reads
asks and decisions in one call, so every reset message has its asks
extracted again. What stays and what can move:

- **Keys and statuses are kept for asks the new pass reports with the same
  kind and addressee.** An ask's key is derived from its source message, kind
  and addressee, never from generated text, so such an ask updates its row in
  place, and the upsert never touches `status`, `closed_at` or `closed_by`:
  an answered or stale ask stays so.
- **Corrections are kept, and outrank the new pass.** A corrected ask is
  exempt from pruning even if the model no longer finds it or reclassifies
  it, and the correction row is untouched.
- **A reclassified uncorrected ask comes back open.** Models disagree across
  runs. If the new pass reports an uncorrected ask with a different kind or
  addressee, its key changes: the old row, answered or not, is withdrawn, and
  a new one is inserted `open`. The observed-event refresh re-closes it only
  where the reply or reaction evidence applies to the new addressee;
  otherwise it can be listed, and notified if recent enough, again.
- **What else can change:** a re-found ask's text, confidence and thread are
  refreshed from the new reply; an *uncorrected* ask the model no longer finds
  is withdrawn; and an ask the first pass missed can appear. A new ask older
  than `NOTIFICATION_MAX_AGE_HOURS` is never notified.

`tests/integration/test_decisions_backfill.py` holds the first three, the
window, and the command's scope and refusal.

## Scheduled tasks

With `SCHEDULED_TASKS_ENABLED=true`, a person can have a question asked on
their behalf between once an hour and once a day, and be sent the answer in a
direct message.

| | |
|---|---|
| `SCHEDULED_TASKS_ENABLED` | Off by default. Bot only |
| `SCHEDULED_TASKS_PER_PERSON` | How many each person may keep (default 5) |
| `SCHEDULED_SWEEP_SECONDS` | How often due tasks are looked for (default 300) |

`/schedule create`, `/schedule list` and `/schedule delete`, each showing and
changing only the caller's own tasks.

### What it costs

**Every run is a full reasoning run** — retrieval, at least one model call,
possibly tool calls. `people × tasks × 24` is the daily ceiling for hourly
tasks. The per-person cap and the one-hour floor bound that; **neither is a
budget**, and a deployment that needs a spending limit needs one of its own.

Turn this on deliberately, after doing that arithmetic for your team size.

### Silence is the design

A run that finds nothing sends nothing. An hourly "I found nothing" is what
makes somebody mute the assistant — and muting it also silences the obligation
notifications they do need, so being chatty here is paid for by a different
feature.

The cost is that a working-but-quiet task is indistinguishable from a broken
one, which is why `/schedule list` shows when each task last ran and what
happened. "Ran 20 minutes ago, found nothing" is the answer to "is this thing
on?", and without it silence would be a support question.

### What a scheduled run can and cannot do

It goes through the same `AskService` a typed question does, which gives two
properties without new code:

- **Access is resolved at run time.** A task created when somebody could read
  a channel stops drawing on it the moment they cannot.
- **Nothing can act.** There is nobody to approve a state-changing tool, so
  every one is refused — the existing confirmation rule, not a new check.

It *can* reach the external tools, so this deployment will make outbound calls
on a timer with nobody watching. Each is still bounded per run by the budget,
rate limit and egress guard, and still rooted in the words the person typed —
but "a person is present" stops being true.

### When it stops

A person whose direct messages are closed has **all** their tasks stopped, with
the reason shown in `/schedule list`. The obstacle is their settings rather than
any one question, and retrying the rest would be knocking on a door already
shut. Removing a person's data deletes their tasks with it.

## Position alerts

People ask for an alert in words, in English or Portuguese, and confirm it
with a button. With the switch off, or without `INFURA_KEY`, such a request is
answered that alerts are not available here — never searched for — and
`/alert list` says the feature is off.

| | |
|---|---|
| `ALERTS_ENABLED` | Off by default. Bot only. Also needs `INFURA_KEY` |
| `ALERT_SWEEP_SECONDS` | How often every alert is checked (default 300, at least 60) |

### Creating one

"tell me when my LP goes out of range", "avise quando minha posição sair da
faixa", "me avisa se o health factor do aave cair abaixo de 1,3", "alert me if
my health factor drops below 1.25 on base", "warn me when my LP is within 5% of
the range edge", "avisa quando o BTC passar de 100k", "alert me when ETH goes
above $3,000". The request is recognised before
retrieval and before the positions route (route label `ALERT_CREATE`), with no
model call. The wallet is one the person typed — in this message or one of
their last few questions — or their saved wallet; nothing read from a channel
is ever a candidate. A question about alerting — "does Uniswap notify me
when…", "which app can warn me when…", "como criar um alerta…" — is not a
request and is answered as any question; "can you alert me when…" is. So is
a price asked outright — "what is the BTC price?", "quanto está o ETH?" —
which the market tools answer.
A scheduled task is never an alert request: nobody is there to press Confirm,
so its question is answered as any other.

The chain is then read once, with the positions reader, and the reply lists
exactly what would be watched with the reading right now: each open Uniswap
position with its range and whether it is in range, or each chain with Aave
debt and its current health factor. It says which chains could not be read,
which positions are already watched, and that positions opened later are not
covered. A limit outside 1.05–5.0 is refused with the bounds. **Nothing is
stored until the person presses Confirm**; Cancel, five minutes of silence, or
a press by anybody else creates nothing (the other person is told privately
the prompt is not theirs).

A **price alert** needs no wallet and reads no chain. The assets are the market
tools' closed vocabulary, BTC and ETH; the level is read as either notation
writes it ("100k", "2.500", "2,500", "$3,000", "120 mil"), and a level with no
direction ("when BTC hits 100k") is taken as the side the price is not on yet.
The confirmation shows the price now from CoinGecko with its quote time, and
says so when the price is already past the level.

A **near-edge alert** is a range alert with a distance, 1–50% ("near the edge"
alone means 5%). The confirmation shows how far each position is from its
nearer edge now — "3.4% from the upper edge" — measured on the price as the
range is quoted. Asked on a position already watched, it adds the distance to
that alert rather than making a second one.

Where the prompt appears follows where it was asked: a reply in a DM, a reply
in the channel for a mention, ephemeral for `/ask`. The wallet address is shown
only in a DM, as for saved wallets.

`/alert list` shows each alert, its state and since when, the last check and
the last message, and whether it is failing or stopped. `/alert delete <id>`
stops one, and answers "not yours" and "no such alert" with the same sentence.
Both are private and work in the server and in a DM. Every alert message ends
with `/alert list` and `/alert delete <id>`.

An alert watches one Uniswap v3 or v4 position that was open when it was made
(in range, near the edge if a distance was asked for, out of range, closed),
one chain's Aave health factor against a limit between 1.05 and 5.0, or the
BTC or ETH price against a level. Each person may keep ten, of all kinds
together, separately from scheduled tasks.

### It messages on a change

A range change counts after two consecutive agreeing checks; a health factor
fires on the first check below the limit and re-arms only at the limit plus
0.05. So an alert says "out of range" once, and "back in range" once, and
nothing in between. A near-edge alert warns once on the way from in range to
within its distance, and re-arms, silently, one percentage point further back;
leaving the range and coming back are still told. A price alert fires on the
first check at or past its level in the direction asked, and re-arms, silently,
once the price is 0.5% back on the other side, so a price wobbling on the line
is one message. A closed position (no liquidity, burned or transferred
NFT) is told once and the alert is stopped with the reason kept. A check that
cannot read the chain says nothing and changes nothing; it is counted against
the alert. Messages are in the language the alert was made in, and the health
message says plainly that a five-minute check is not liquidation protection.

### What it costs, and what leaves

No model call and no archive search: every due alert on a chain is read in one
Multicall3 request, so a sweep is about one request per chain — about 864 a day
at the default for a few alerts on three chains. It uses its own rate limiter,
so it never takes spacing from interactive lookups, and reads at most 500 calls
per chain per sweep.

It is also an egress path with nobody asking: each sweep sends the watched
addresses to Infura. The per-question guard has no question to root them in,
so the address is checked once, when the alert is made — the person's saved
wallet or an address they typed — and stored. That is declared in the
`position-alerts` spec and is why the feature is off unless switched on.

Price alerts add one more request per sweep, whatever their number: the
constant CoinGecko request the market tools and the portfolio's ether pricing
already make (`ids=bitcoin,ethereum`), through `Edges.http_transport`, with the
market provider's fresh-only cache and its own rate limiter. It carries nothing
about anybody — not the asset watched, not the level — so it needs no
clearance, and it goes out whether or not `MARKET_TOOLS_ENABLED` is on. That is
declared in the `alert-kinds` change. A sweep with no due price alert sends it
not at all; one that cannot get a fresh price counts a failed check and says
nothing.

Creation reads more than a sweep does: a range request runs the full positions
discovery (two to twenty-five requests per chain, several seconds on Arbitrum
v4), a health request one `getUserAccountData` per chain. That happens once per
request, before the Confirm button, and never on the sweep.

### When it stops

Closed direct messages stop **all** of that person's alerts, as for scheduled
tasks. Forgetting or replacing the saved wallet deletes the alerts on it;
opting out or being removed deletes them all. All three are enforced in the
database, so no path that removes the fact can leave the wallet watched.

## Contact facts, and a saved wallet

A person can tell the assistant six things about themselves: preferred name,
email, phone, preferred language, Ethereum wallet, Bitcoin wallet. Set from
their own message, never learned from a channel, and each validated for the
shape its kind requires.

**Email, phone and both wallets are direct-message only.** A wallet address is
public on its chain; what is private is that it belongs to a particular person,
and a channel reply naming it makes that link for everyone present.

### What a saved wallet means for egress

This is the part worth understanding before enabling wallet tools.

An outbound query must be rooted in the asker's own words. A saved address is
not in the question they just typed, so "what's my wallet balance?" would
otherwise be refused for want of an address.

The rule is not relaxed. The root set widens from *the words of this question*
to *the words this person wrote about themselves*, and a saved fact qualifies
because they typed it when they set it. Three things keep that narrow:

- The check is **containment against the exact strings the store holds for that
  asker**, not a label. A caller that supplies the wrong address gets a
  refusal, so the route spelling an address into the question is a hint and
  never the authorisation.
- Values are read **per person**, so one person's saved wallet can never
  authorise another's lookup.
- The **content gate runs first**. An address that appears in a channel message
  is refused even when it happens to match something the asker saved.

What it does mean: a person's saved address leaves the server when they ask
about their own balance. That is what they asked for by saving it.

## A preferred currency

A person can say which currency they want money shown in besides US dollars
(*minha moeda é o real*, *my currency is euro*, *moeda preferida: BRL*, or
*uso reais* inside an introduction). It is stored as an ISO 4217 code, in the
`preferred_currency` kind migration 0027 adds; it is not private, and is shown
in a channel like the preferred language. *Uso* / *I use* takes a currency
name, never a bare code or a coin: *I use PHP* and *eu uso bitcoin* are
ordinary messages. A code works where only money is meant (*prefiro ver em
BRL*, *moeda preferida: BRL*).

Every figure the assistant reads is in dollars. With a preference saved, each
dollar figure it renders — a BTC/ETH price, a balance, a pool, an Aave
position, a portfolio total, wallet activity, a price or health alert — is
followed by the same figure in that currency, multiplied in code, with a
footnote naming the rate. A market answer carries the converted figure in the
tool result itself, so no model converts anything.

### What leaves, and where

One request, to the host the conversion tool already uses:
`api.frankfurter.dev/v1/latest?from=USD&to=BRL`. The two codes are members of
a closed set — the currencies Frankfurter publishes an ECB rate for, which is
also the set a preference may name — and no amount and nothing else about the
person goes with them. It is made through the market FX provider class and
`Edges.http_transport`, whether or not `MARKET_TOOLS_ENABLED` is on (with the
market tools off it is a provider of the same class that is never registered as
a tool). It is not routed through the egress guard: nobody asked a question the
code could be rooted in; the person's saved preference is the authorisation.

The rate is cached in the process for ten minutes, so an answer reads it once
and the next answers usually not at all. A rate that cannot be read — the host
down, a timeout, an unexpected reply — leaves the answer in dollars alone; it
is never guessed and never fails the answer. Frankfurter's rate is a daily
reference rate, not a live one, and the footnote says so.

A price alert's message reads the owner's preference from the fact store when
it fires; the level they set stays in dollars, as they set it. An alert
proposal (the Confirm step) is in dollars only.

## Knowing the time

Every answering prompt carries the current date and time in **UTC**, labelled.
Before this, nothing did: time-scoped questions worked where SQL filtered them
("what did people ask me today" is a `WHERE` clause), but a model judging
whether something was recent, or asked the date outright, had only its training
data.

The clock is context, not evidence. It is never a citation, and it cannot make
a claim answerable that the evidence does not support — the grounding rule is
unchanged.

One clock, stated as UTC, rather than per-person timezones: an answer that
names a time says which one, which is the property that matters.

Asking the date outright has its own route, decided before retrieval, and is
answered without a model call. Carrying the clock in the prompt is not enough
on its own: the first version did exactly that and still answered "I couldn't
find anything about that in the messages you can see", because the question
went to the corpus, found no evidence, and the grounding rule refused — as it
should for a question the corpus cannot answer.

The route is anchored to questions whose whole content is the clock. "What was
decided today" and "what time did the deploy finish" are questions about the
corpus that merely contain the word, and they still go there.

## Voice questions

A person can send the bot a Discord voice message — or an audio file — in a
DM, and it is answered exactly as if they had typed the words: every route
(facts, what someone said, catch-up, obligations, decisions, crypto, the
reasoning loop), the progress note, and conversation memory. The reply opens
with a small quoted line of what was understood, `-# 🎤 "…"`, so a mishearing is
visible before the answer is trusted. Channels are unchanged: a voice note that
mentions the bot in a channel gets the ordinary capabilities reply.

| | |
|---|---|
| `VOICE_QUESTIONS_ENABLED` | Off by default. Bot only. Needs `MEDIA_API_KEY`, or the bot refuses to start |
| `MEDIA_BASE_URL`, `MEDIA_API_KEY`, `MEDIA_AUDIO_MODEL` | Where audio is sent: `{MEDIA_BASE_URL}/audio/transcriptions`, default OpenAI `gpt-4o-mini-transcribe` |
| `VOICE_MAX_SECONDS`, `VOICE_MAX_BYTES` | Longest and largest accepted: 120 s and 10 MB |
| `VOICE_PERSON_MONTHLY_MINUTES` | Per person per calendar month (UTC): 60 |
| `MEDIA_AUDIO_MONTHLY_MINUTES` | Everybody, per month: 1500. Shared with any later channel transcription |
| `MEDIA_TIMEOUT_SECONDS` | One download, and separately one transcription: 30 |

Switched off, a voice message gets one fixed reply — voice is not enabled here,
please type — and nothing is downloaded.

### What is checked, in order

1. **The attachment, from its metadata alone.** Exactly one attachment; a
   declared type of `audio/ogg` (a Discord voice message; MP3, M4A, WAV and WebM
   uploads are refused, because their length cannot be counted); a URL on
   `https://cdn.discordapp.com` or `https://media.discordapp.net` and nowhere
   else; within the byte limit and, when the client declares one, the duration
   limit.
2. **The person and the month.** An opted-out person is refused. Then the
   minutes are charged against both caps in one transaction under an advisory
   lock — by the duration the client declares, rounded up, or the whole
   `VOICE_MAX_SECONDS` for an uploaded file that declares none. Over either cap
   the person is told which one (their own, or the server's), and nothing is
   fetched or sent. A charge is not refunded if a later step fails: the cap is
   the ceiling on the bill, not an estimate of it.
3. **The download**, through the process's HTTP transport, redirects not
   followed, abandoned past `VOICE_MAX_BYTES`. Its real length is then counted
   from its Opus packets. The declared duration is written by the uploading
   client and bounds nothing, so audio longer than `VOICE_MAX_SECONDS`, or
   longer than it was charged, is refused here and never sent. Anything that
   is not a single Ogg Opus stream is not sent either. This is what makes the
   caps a ceiling on what is transcribed.
4. **The transcription**: one multipart POST, no retries. The transcript,
   flattened to one line, is the question; one longer than 4000 characters (a
   typed message's limit) is refused as too long.

A person already at their hourly question limit is told so before anything is
downloaded, so minutes are not spent on a question that would be refused.

Every failure from step 3 on is one reply — "I couldn't understand the audio,
please try again or type" — never an exception. All fixed replies are in the
person's saved language (Portuguese or English; English when none is saved).

### What is kept, and what leaves

The audio is never stored: downloaded, sent, discarded. The transcript is kept
only where typed text is kept — as the question in conversation memory — and
leaves only where typed text leaves. `media_usage` (migration 0025) holds seconds
per person, month and purpose (`question` now, `channel` reserved for channel
transcription), and nothing else. Logs record the refusal reason and the
transcript's length, never its words.

What does leave is the recording itself, to `MEDIA_BASE_URL`. With the default
that is OpenAI, a third party receiving people's voices; point it at a
self-hosted OpenAI-compatible Whisper to keep voice inside the network.

### What it costs

`gpt-4o-mini-transcribe` is about $0.003 per minute (check current pricing). At
the default ceiling of 1,500 minutes a month that is at most about $4.50; a
person at their 60-minute cap costs about $0.18.


## Channel media

Voice notes and images posted in indexed channels are recorded as pending
`message_media` rows (migration 0026), for a later worker to transcribe or
describe. Today only the recording exists: nothing is downloaded, no model is
called, and nothing about a voice note or image is searchable yet.

| | |
|---|---|
| `MEDIA_ENABLED_AT` | Unset by default: nothing is recorded. An ISO timestamp (UTC when it has no zone); messages created at or after it are recorded. Ingest only |
| `MEDIA_BACKFILL_DAYS` | Default 0. Days before `MEDIA_ENABLED_AT` also recorded, for messages ingest writes from now on (see below). History already imported is not re-read |

A moment rather than a switch, on purpose: members posted their earlier voice
notes without expecting them to be transcribed, so set it to when they were
told, and leave the backfill at 0 unless they were told that too.

Both are checked only when ingest writes a message: live, on an edit, in a
channel's first backfill (one newly indexed, or returned to scope), or when
reconciliation finds a message posted while ingest was down. A message already
imported and never edited is not written again, so on an existing deployment
`MEDIA_BACKFILL_DAYS` records nothing of the channels already indexed: backfill
has finished with their history, and reconciliation rewrites only what
changed. There is no command yet to re-read a window of history for media.

### What is recorded

One row per attachment whose declared type is `audio/ogg`, `audio/mpeg`,
`audio/mp4`, `audio/wav`, `audio/webm`, `image/png`, `image/jpeg` or
`image/webp`, and whose URL is on `https://cdn.discordapp.com` or
`https://media.discordapp.net`. A voice note (the message carries Discord's
`IS_VOICE_MESSAGE` flag) is `voice`; any other audio is `audio`. Each row holds
the declared type, size, filename, the duration Discord declares on a voice
note, and the signed CDN URL — never the bytes.

- **Only stored messages.** A row references its message, so DMs, private
  threads, channels out of scope, bot messages and opted-out authors — none of
  which become a stored message — can have none.
- **Re-writing refreshes, never resets.** When a message is written again (an
  edit, a gateway re-delivery) a pending row takes its new URL and keeps its
  status and attempts. That is rare, so the stored URL — which expires in about
  a day — is usually stale by the time anything reads it; the transcription
  worker (not yet built) re-fetches the message for a fresh one right before
  downloading.
- **Edits.** An edit that removes an attachment removes its row.
- **Deletion.** Deleting a message withdraws its rows in the same transaction:
  status `withdrawn`, any derived text and the URL cleared.
- **Retention, opt-out, channel purge.** All three delete messages, and the
  rows go with them by cascade.
