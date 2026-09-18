# Operations — retention, opt-out, and CI

CyberFriend builds a searchable index of everything said in the channels you
point it at. That is the feature. It is also the reason two controls on this
page are a precondition for running it anywhere real, rather than nice-to-haves:
without them the bot is a permanent, queryable archive of your team's
conversations that nobody consented to and nobody can leave.

## Retention

`RetentionService` purges messages, the windows built over them, extracted asks,
the document fetch log, and document entries older than a configured window.

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
extracted from and addressed to them, their reactions, the mention index rows
pointing at them — **and the documents they uploaded**. An opt-out that covers
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
5. `alembic upgrade head`, then the full `pytest tests`

A pgvector Postgres runs as a service container, because the integration tests
are where the permission predicate is proved against a real planner and a real
approximate index — and those tests *skip* rather than fail when no database is
reachable. A CI job without one is green and proves nothing.

No OpenAI key is supplied. The tests that would spend money skip themselves, and
a pull request from a fork must not be able to bill you.

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
  command and no MCP tool; an opt-out today is an operator running
  `OptOutService.opt_out` on somebody's behalf. A consent control that requires
  filing a ticket is a weak one.
- **`OptOutService.filter_messages` is not called by ingestion.** It is an
  optimisation, not the guarantee — the trigger is the guarantee — but until it
  is wired into `IngestService`, every excluded message costs a round trip to
  the database to be rejected.

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
what a `0x` address holds on Ethereum and Base: the native ETH balance and a
named set of ERC-20 tokens, each with its USD value from the same price source
the market tools use. One key covers both chains.

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
address is answered by asking for one, not by searching.

### What it cannot see

JSON-RPC cannot enumerate holdings. `eth_getBalance` gives the native balance;
every token requires knowing a contract to call `balanceOf` on. So the token set
is **named, not discovered** — currently USDC, USDT, DAI and WETH on Ethereum,
and USDC, DAI and WETH on Base. A token nobody listed is invisible to the
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
holding nothing; one endpoint failing still reports the other.

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
