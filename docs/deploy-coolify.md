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

## 2. Application

Create an **Application** from this repository, build pack **Docker Compose**,
compose file `/docker-compose.yml`.

Two services come from one image:

| Service  | Role                                   | Domain |
|----------|----------------------------------------|--------|
| `ingest` | Discord gateway client plus workers    | none   |
| `mcp`    | HTTP retrieval interface               | FQDN   |

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

- Confirm the Cyberdyne server reaches `api.openai.com`. Every indexed message
  is embedded through it, so no reachability means no retrieval.
- Confirm the MESSAGE_CONTENT intent is enabled in the Discord Developer
  Portal. Since 2026-06-10 the threshold for review is 10,000 reachable users
  rather than 100 servers, so for one internal guild this is a toggle.
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
