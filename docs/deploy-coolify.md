# Deploying CyberFriend on Coolify (Cyberdyne)

Target: `https://coolify.cyberdynecorp.ai` (Coolify 4.3.19, single server
`localhost`). Use `--server cyberdyne` with the Coolify CLI helper.

## 1. Database — must be pgvector, not plain Postgres

Create a **PostgreSQL with pgvector** database resource. Coolify offers this as
a first-class type; do not use the plain PostgreSQL type, whose `postgres:16`
image does not carry the extension. The existing `cyberspace-postgres` on this
instance is stock `postgres:16` and belongs to another project — do not reuse it.

After creation:

- Enable **scheduled backups**. The corpus is rebuildable by re-backfilling
  Discord, but a full backfill of an old server is slow, so backups are worth
  having.
- Note the internal connection URL. It resolves only inside the Coolify network,
  which is what we want: the database gets no public port.

The migration creates the extension itself (`CREATE EXTENSION IF NOT EXISTS
vector`), which requires the image to carry it — hence the database type above.

### Connect the application to the predefined network

**Required, and not stored in the repository.** On the application's settings,
enable **Connect To Predefined Network**.

A Docker Compose application is placed on its own isolated network, while a
standalone database lives on the shared `coolify` network. Without this the
containers cannot resolve the database host at all, and the symptom is
indirect: the deploy fails after roughly two minutes with `service "migrate"
didn't complete successfully`, because the migration job exhausts its
connection retries. Nothing in the log names the network.

Via the API:

```bash
curl -X PATCH -H "Authorization: Bearer $COOLIFY_CYBERDYNE_TOKEN" \
  -H "Content-Type: application/json" -d '{"connect_to_docker_network":true}' \
  "$COOLIFY_CYBERDYNE_URL/api/v1/applications/<app-uuid>"
```

## 2. Application

Create an **Application** from this repository, build pack **Docker Compose**,
compose file `/docker-compose.yml`.

Two services come from one image:

| Service  | Role                                   | Domain |
|----------|----------------------------------------|--------|
| `ingest` | Discord gateway client plus workers    | none   |
| `bot`    | Conversational surface (mentions, DMs, `/ask`) | none |
| `mcp`    | HTTP retrieval interface               | FQDN   |

`bot` and `ingest` each hold their own gateway connection and each run one
replica.

Assign a domain to `mcp` only. `ingest` must not be publicly reachable; it has
a health port for Coolify's check and nothing else.

### `ingest` runs exactly one replica

This is not a scaling preference. Two containers sharing one bot token both
identify to the Discord gateway and each receive every message, so the corpus
doubles. Discord raises no error and nothing in the logs looks wrong — the row
counts simply drift. Do not scale this service.

## 3. Environment variables

Set at **runtime** scope, not build scope — the image must contain no secrets.

| Key | Notes |
|-----|-------|
| `DISCORD_TOKEN` | Bot token. Requires the MESSAGE_CONTENT privileged intent enabled. |
| `DISCORD_GUILD_ID` | The server being indexed. |
| `INDEXED_CHANNEL_IDS` | Space- or comma-separated. **Empty indexes nothing** — indexing is opt-in. |
| `DATABASE_URL` | Internal URL of the pgvector database, `postgresql+asyncpg://…`. |
| `LLM_BASE_URL` | `https://api.openai.com/v1`. |
| `LLM_API_KEY` | OpenAI key. |
| `EMBEDDING_MODEL` | `text-embedding-3-small`. |
| `EMBEDDING_DIMENSIONS` | `1536`. Changing this later is a reindex, not a swap. |
| `MCP_TOKENS` | Bearer tokens, each bound to one person. See below. |
| `VOICE_QUESTIONS_ENABLED` | Optional, default `false`. Voice messages sent to the bot in a DM are transcribed and answered. Bot only. Needs `MEDIA_API_KEY`; enabling without it stops the bot at boot. |
| `MEDIA_API_KEY` | The transcription endpoint's key. Separate from `LLM_API_KEY` on purpose: where voices go is its own decision. |
| `MEDIA_BASE_URL` | Optional, default `https://api.openai.com/v1`. Any OpenAI-compatible endpoint serving `/audio/transcriptions`. |
| `MEDIA_AUDIO_MODEL` | Optional, default `gpt-4o-mini-transcribe`. |
| `VOICE_MAX_SECONDS`, `VOICE_MAX_BYTES` | Optional, defaults `120` and `10000000`. Longest and largest voice question accepted. |
| `VOICE_PERSON_MONTHLY_MINUTES` | Optional, default `60`. Hard cap per person per calendar month (UTC). |
| `MEDIA_AUDIO_MONTHLY_MINUTES` | Optional, default `1500`. Hard cap for the whole deployment per month: the ceiling on the transcription bill. |
| `MEDIA_TIMEOUT_SECONDS` | Optional, default `30`. Bound on one download and on one transcription. |
| `MEDIA_ENABLED_AT` | Optional, unset by default. Ingest only. From this moment (ISO timestamp, UTC when unzoned), voice notes and images posted in indexed channels are recorded as pending rows: metadata only, nothing downloaded. Unset records nothing. |
| `MEDIA_BACKFILL_DAYS` | Optional, default `0`. Days before `MEDIA_ENABLED_AT` also recorded, but only for messages ingest writes from now on: a newly indexed channel's backfill, edits, messages posted while ingest was down. History already imported is not re-read. |
| `ANSWER_TIMEZONE` | Optional. IANA zone whose days "ontem" and "semana passada" mean; defaults to `America/Sao_Paulo`. An unknown name stops the bot at boot. |

### MCP tokens are bound to a person

The `mcp` service is publicly reachable, so the bearer token *determines* whose
view the caller receives; a request cannot name a viewer. Issue one token per
person. A leaked token then exposes that one person's readable channels rather
than the entire corpus.

Consequences worth being explicit about:

- A token is as powerful as the person it maps to. Treat one belonging to
  someone with broad channel access accordingly.
- Rotate by replacing that person's entry; other tokens are unaffected.
- Tokens are stored hashed. The plaintext exists only in Coolify's environment
  and wherever the holder keeps it.

## 4. Before the first deploy

If you do not yet have a bot token, server ID and channel IDs, work through
[discord-setup.md](discord-setup.md) first — the deploy cannot be verified
without them.

- Confirm the Cyberdyne server reaches `api.openai.com`. Every indexed message
  is embedded through it, so no reachability means no retrieval.
- Confirm **both** privileged intents are enabled in the Discord Developer
  Portal: **MESSAGE_CONTENT** (to read messages) and **SERVER MEMBERS** (to
  enumerate who can read a channel, which is what makes audience scoping
  exact rather than an approximation over role sets). Since 2026-06-10 the
  threshold for review is 10,000 reachable users rather than 100 servers, so
  for one internal guild both are toggles.

  Without SERVER MEMBERS the bot fails closed rather than leaking: audiences
  resolve to empty and public answers cite nothing. If the bot answers in a
  channel but never cites anything, check this first.
- Invite the bot with `View Channel` and `Read Message History` on the channels
  to be indexed, and nothing more. It never needs to post in them.

## 5. Verify after deploying

```bash
CLI="python3 ~/.claude/skills/coolify/coolify_cli.py --server cyberdyne"
$CLI apps                      # find the CyberFriend uuid
$CLI logs <app-uuid> --lines 100
```

A deployment that reports `finished` while the container restarts repeatedly
looks healthy in the deployment record; the crash loop only shows in `logs`.

End-to-end check:

1. `/ready` on the `ingest` health port reports `gateway_connected: true`.
2. Post a message in an indexed scratch channel; confirm it reaches the corpus.
3. Query the `mcp` domain over HTTPS with a bearer token and confirm the message
   comes back with a citation link that resolves in Discord.
4. Confirm a caller with person A's token cannot retrieve content only person B
   can read.

## 6. Operational notes

- **Backfill is slow and newest-first**, so recent history becomes queryable
  early while older history continues to import. Backfill lag is reported on
  the health endpoint.
- **Embedding backlog** is reported there too. A backlog that grows rather than
  drains means the embedding endpoint is failing or rate-limiting; retrieval
  quality degrades quietly before anything else looks wrong.
- **Deleting a channel from `INDEXED_CHANNEL_IDS`** stops ingestion but does not
  purge existing content on its own; run the purge task for that channel.


## 7. What actually went wrong the first time

Seven issues surfaced during the first deployment, none of which the test
suite could have caught. They are recorded here because most of them look
like application faults from the outside.

| Symptom | Real cause |
|---|---|
| Platform rejects an env var with 422 | The variable is not declared in `docker-compose.yml`. Coolify only accepts variables the compose file mentions, so a setting absent there is not merely undocumented -- it cannot be set. |
| Containers crash-loop with `ModuleNotFoundError` | The image shipped an empty package. A stub is installed first to cache the dependency layer; because the project version does not change, `uv` skipped the real install. Fixed with `--reinstall-package` plus a build-time import. |
| `service "migrate" didn't complete successfully`, fast | The migration job loaded application settings, which validate the Discord credentials. A first deploy has none, so the schema could not be created until a bot token existed. |
| `service "migrate" didn't complete successfully`, after ~2 min | The application is not on the predefined network and cannot reach the database. See section 1. |
| `Input should be a valid integer` for a setting nobody touched | Coolify writes a blank for every compose variable left unfilled, and a blank is not the same as unset. Blanks are now dropped before validation. |

Two lessons worth keeping:

- **The deployment log does not carry container output.** When a container
  fails, read its logs or reproduce the image locally. Inferring the cause
  from the one variable you know is missing produces confident wrong answers.
- **A build that succeeds is not a build that works.** The empty-package
  image built cleanly for several deploys.
