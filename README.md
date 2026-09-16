# CyberFriend

An ACL-aware chat-memory agent for Discord. It indexes channel history into
Postgres with pgvector and answers retrospective questions — *what did people
ask me today*, *what happened in #infra this week* — filtered to the channels
the person asking is actually permitted to read.

Slack support is planned and deliberately deferred; see `openspec/changes/`.

## Status

Deployed and answering questions. Specification is validated and
implementation continues against it.
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

Setting up the Discord side — application, token, intents, invite, and the
server and channel IDs — is walked through in
[docs/discord-setup.md](docs/discord-setup.md).

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

Runs on Coolify (Cyberdyne) as a `dockercompose` resource against a
Coolify-managed **PostgreSQL with pgvector** database. The stock `postgres`
image does not carry the extension.

| Service | Role | Public domain |
|---|---|---|
| `migrate` | Applies migrations once per deploy, then exits | — |
| `ingest` | Discord gateway: capture, backfill, windowing, embeddings, extraction | — |
| `bot` | Answers questions in Discord | — |
| `mcp` | MCP interface to the corpus | yes |
| `admin` | Operator console | yes |

Three settings in the Coolify application are easy to miss and each has broken
this deployment once:

- **Connect To Predefined Network** must be on, or the services cannot reach
  the database. The symptom is `migrate` failing about two minutes into the
  deploy, with nothing in the log naming the network.
- **Every setting must be declared in `docker-compose.yml`.** Coolify refuses a
  variable the compose file does not mention, and an operator setting it
  silently gets the default.
- **`CHAT_MODEL_CAPABILITIES` narrows** what the model is assumed to support.
  `chat` is always included, but anything else you omit is treated as absent,
  so enabling tool calling means listing it alongside `structured_output`.

Do not trust Coolify's `running:healthy` on its own. The healthchecks now test
readiness — whether each service actually reached Discord — but Coolify's
status reflects container state. To confirm the bot really connected after a
deploy, check that Discord's IDENTIFY count dropped:

```bash
curl -s -H "Authorization: Bot $DISCORD_TOKEN" \
  https://discord.com/api/v10/gateway/bot | jq .session_start_limit.remaining
```

`ingest` runs exactly one replica. Two containers sharing a bot token both
identify to the gateway and ingest every message twice; Discord does not
error, the corpus just silently doubles.

## Admin console

A web console for configuring the agent: federated MCP servers and their tool
allowlist, indexed channels, retention, per-person opt-outs, and MCP tokens,
with read-only views of health, ingestion progress and the change record.

It runs as the `admin` service and is published at its own domain, for example
`https://admin-<app-uuid>.coolify.cyberdynecorp.ai`.

### What the service holds, and what it does not

The `admin` service is given **database credentials and nothing else** — no
Discord token, no model key, no SerpApi key, no Coolify credential. It changes
configuration by writing to the database; it cannot redeploy, impersonate the
bot, or call a model.

It also cannot read the corpus. No endpoint returns message, document or ask
content; status views report counts and timings only.

### Issuing the first credential

The console has no sign-up and cannot mint its own tokens — a console that can
issue admin credentials has no root of trust. Credentials are issued from a
shell inside the `admin` container. In Coolify: open the application, choose
the **admin** service, and open its **Terminal**.

```bash
# One credential per person. The name is what every change they make is
# attributed to in the change record.
python -m chatmemory.admin.issue_token issue leonardo --label laptop

# Revoke one operator without affecting anyone else
python -m chatmemory.admin.issue_token revoke leonardo

# Who holds a credential, and the record of issuing and revoking
python -m chatmemory.admin.issue_token list
python -m chatmemory.admin.issue_token log --limit 20
```

`issue` prints the token **once**. It is stored only as a hash, so a lost
token cannot be recovered — revoke it and issue a new one.

Paste the token into the console to sign in. It is held in memory only and is
gone when the tab closes; nothing is written to browser storage.

**Do not share a credential between people.** Nothing technical can detect it,
and the change record will name the wrong person for every edit.

### Enabling a tool that changes things

Tools are read-only unless an operator declares otherwise. Enabling a tool
that modifies state — closing an issue, posting a comment — requires typing the
tool's name to confirm, and is recorded as an escalation of what the agent may
do, separately from ordinary edits.

Even once enabled, every call to such a tool is shown to the person who asked,
with its exact arguments, and runs only if they approve it.

### Which settings live where

| Setting | Where | Editable in the console |
|---|---|---|
| `DISCORD_TOKEN`, `LLM_API_KEY`, `SERPAPI_KEY`, `DATABASE_URL` | Environment only | No — never readable or writable |
| Indexed channels, retention, opt-outs | Database, falling back to environment | Yes |
| Federated servers and tool allowlist | Database, falling back to environment | Yes |
| MCP tokens | Database | Yes |

The console shows, for every setting, whether its value came from the
database, the environment, or a default. A value edited in the console but
overridden elsewhere is otherwise indistinguishable from one that did not save.

> **Current limitation.** The console writes configuration to the database,
> but the running `bot` and `ingest` processes do not yet read it back — they
> still take their settings from the environment at startup. Until that is
> wired, changes made in the console are recorded but **do not take effect**;
> change the environment variable in Coolify and redeploy instead.

### Local development

```bash
cd console && npm ci && npm run dev    # Vite dev server against a local API
python -m chatmemory.entrypoints.admin # the API, which also serves console/dist
```

`npm run build` type-checks before bundling, so a type error fails the build
rather than shipping a console that breaks in the browser. The Docker image
builds the console in a separate Node stage and copies only the compiled
output, so the runtime image carries no Node toolchain.

## Tests

```bash
ruff check . && mypy && pytest
```

Integration tests need a live pgvector database:

```bash
docker compose -f docker-compose.dev.yml up -d
alembic upgrade head
pytest tests/integration
```

They skip rather than fail when no database is reachable, so a missing
database is never reported as a defect in the code.
