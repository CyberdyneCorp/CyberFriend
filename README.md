# CyberFriend

An ACL-aware chat-memory agent for Discord. It indexes channel history into
Postgres with pgvector and answers retrospective questions — *what did people
ask me today*, *what happened in #infra this week* — filtered to the channels
the person asking is actually permitted to read.

Slack support is planned and deliberately deferred; see `openspec/changes/`.

## Status

Specification is complete and validated; implementation is in progress.
Work is spec-driven — read `openspec/changes/*/proposal.md` before changing
behaviour, and `openspec/changes/*/design.md` for why things are shaped as
they are.

```bash
openspec list                    # active changes and task progress
openspec validate --all --strict
```

## Two things to know before running this anywhere real

**It creates a permanent searchable archive of everything said in indexed
channels.** That needs a retention window, disclosure to the team, and an
opt-out. Indexing is opt-in per channel for this reason — `INDEXED_CHANNEL_IDS`
is empty by default and indexes nothing.

**No bot can read direct messages between people, on any platform.** Questions
like "what did people ask me today" cover indexed channels and DMs sent to the
bot, and nothing else.

## Local development

```bash
uv venv && uv pip install -e '.[dev]'
docker compose -f docker-compose.dev.yml up -d
cp .env.example .env        # fill in DISCORD_TOKEN, LLM_API_KEY
alembic upgrade head
python -m chatmemory.entrypoints.ingest
```

Discord requires **two** privileged intents for this application:

- **MESSAGE_CONTENT** — to read what people actually said.
- **SERVER MEMBERS** — to enumerate who can read a channel. This is what makes
  audience scoping exact. Comparing role sets instead would miss per-member
  channel overwrites, and a member individually denied a channel is precisely
  the case that turns a public answer into a disclosure.

Since 2026-06-10 the review threshold is 10,000 reachable users rather than
100 servers, so for a single internal guild both are toggles in the Developer
Portal rather than an application.

Without the members intent the bot fails closed: the audience resolves to
empty and public answers cite nothing, rather than silently citing everything.

## Deployment

Runs on Coolify (Cyberdyne) as a `dockercompose` resource with two services —
`ingest` (Discord gateway, no domain) and `mcp` (HTTP, the only service given
an FQDN) — against a Coolify-managed **PostgreSQL with pgvector** database.
The stock `postgres` image does not carry the extension.

`ingest` runs exactly one replica. Two containers sharing a bot token both
identify to the gateway and ingest every message twice; Discord does not
error, the corpus just silently doubles.

## Tests

```bash
ruff check . && mypy && pytest
```
