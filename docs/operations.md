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
