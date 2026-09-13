## 1. Project scaffolding

- [ ] 1.1 Initialise the Python project with `uv` (Python 3.11+), `pyproject.toml`, and the `src/chatmemory` package layout
- [ ] 1.2 Add dependencies: `discord.py`, `asyncpg`/`sqlalchemy`, `pgvector`, `openai`, `mcp`, `pydantic-settings`, `pytest`, `pytest-asyncio`
- [ ] 1.3 Add `docker-compose.yml` running Postgres with the `pgvector` extension, and a `.env.example` documenting every setting
- [ ] 1.4 Add config loading: Discord token, database URL, LLM `base_url`/API key/embedding model + dimensionality, indexed-channel scope
- [ ] 1.5 Add `.gitignore`, `README.md`, and lint/type/test commands (`ruff`, `mypy`, `pytest`)

## 2. Domain and ports

- [ ] 2.1 Define domain models: `Person`, `Channel`, `Message`, `Window`, `Viewer`, `SearchResult`, `Citation`
- [ ] 2.2 Define the `Store` port (persistence and idempotent upsert by platform message ID)
- [ ] 2.3 Define the `AclResolver` port returning a viewer's visible channel set
- [ ] 2.4 Define the `SearchBackend` port — **`viewer` is a required parameter on every content-returning method**, so unfiltered retrieval cannot be expressed
- [ ] 2.5 Define `SearchQuery` to carry **intent only** (terms, time range, author, channel preference); the readable-channel predicate must NOT be a field on it, so a future query-rewriting loop cannot reach it
- [ ] 2.6 Make `viewer` **required with no default** on the retrieval context — a context constructed without one must fail to construct, not default to empty
- [ ] 2.7 Test: assert by reflection that no rewritable query type exposes a channel-permission field
- [ ] 2.8 Define the `ChatSource` and `EmbeddingClient` ports
- [ ] 2.9 Assert in tests that no module under `domain/` or `ports/` imports `discord` or `openai`

## 3. Storage schema

- [ ] 3.1 Write the initial migration: `person`, `person_platform_id`, `channel`, `message`, `message_mention`, `window`, `window_message`, `ingest_cursor`
- [ ] 3.2 Add `deleted_at` tombstone columns and partial indexes excluding tombstoned rows
- [ ] 3.3 Add the `tsvector` column and GIN index for lexical search
- [ ] 3.4 Add the `vector` column (dimension from config) and its ANN index
- [ ] 3.5 Implement the Postgres `Store` adapter with idempotent upserts keyed on **platform message ID + edit timestamp only** — never a content hash compared across parsed/raw domains
- [ ] 3.6 Denormalise `channel_id` and timestamp onto `window` rows so the permission predicate constrains the index scan rather than filtering its output
- [ ] 3.7 Set `hnsw.iterative_scan = relaxed_order` on the session/connection; it defaults to `off` and guarantees under-return with a selective filter
- [ ] 3.8 Test: ingesting the same message twice produces exactly one row

## 4. Discord ingestion

- [ ] 4.1 Implement the gateway client with the `MESSAGE_CONTENT` intent and live `on_message` capture
- [ ] 4.2 Implement canonical identity resolution — create-or-attach a person per platform account
- [ ] 4.3 Implement mention capture into `message_mention`
- [ ] 4.4 Implement edit handling so stored content becomes the edited text
- [ ] 4.5 Implement delete handling, writing tombstones to the message and its windows
- [ ] 4.6 Implement paginated backfill, newest-first, with per-channel watermarks
- [ ] 4.7 Handle 429s by honouring the platform's retry interval without losing backfill position
- [ ] 4.8 Implement the periodic reconciliation pass that catches edits and deletes missed while offline
- [ ] 4.9 Enforce indexing scope: skip non-indexed channels, and purge a channel's content when it leaves scope
- [ ] 4.10 Test: interrupt a backfill mid-channel, restart, assert no gaps and no duplicates
- [ ] 4.11 Test: delete a message, assert its content is unreachable through every retrieval path

## 5. Windowing and embeddings

- [ ] 5.1 Implement window construction — thread-aware, with a configurable message span and silence-gap split, sized in **tokens using the embedding model's tokenizer** (not characters)
- [ ] 5.2 Implement incremental rebuild so an edited or deleted message re-forms only its affected windows
- [ ] 5.3 Implement the OpenAI-compatible `EmbeddingClient` against the configured `base_url`, with batching and retry
- [ ] 5.4 Implement the embedding backlog worker over windows lacking a current embedding
- [ ] 5.5 Test: a window containing a deleted message excludes that message's text once rebuilt

## 6. ACL resolution

- [ ] 6.1 Implement the Discord `AclResolver` using `channel.permissions_for(member)`, requiring **both** `view_channel` and `read_message_history`
- [ ] 6.2 Handle the non-member / unresolvable-viewer case by returning an empty visible set
- [ ] 6.3 Add short-TTL caching of resolved sets, invalidated on member and channel update events
- [ ] 6.4 Test: member with `view_channel` but not `read_message_history` is denied — the easiest case to get wrong
- [ ] 6.5 Test: revoking a role removes the channel from the next query with no reindexing

## 7. Retrieval

- [ ] 7.1 Implement lexical search over the `tsvector` index, viewer- and time-filtered in the same query
- [ ] 7.2 Implement vector search over window embeddings, viewer- and time-filtered in the same query
- [ ] 7.3 Implement Reciprocal Rank Fusion over both result sets
- [ ] 7.4 Tag every score with the method that produced it (`relevance_source`); RRF's 0.016 means *first place*, and any consumer applying a threshold must check provenance first
- [ ] 7.5 Implement citation construction producing resolvable Discord message links
- [ ] 7.6 Implement `thread_context` retrieval around a cited message, viewer-filtered
- [ ] 7.7 **Test the core invariant at the repository layer**: a viewer who cannot read a channel gets zero rows from it even when the query terms match it exactly
- [ ] 7.8 Test: a time-bounded query returns nothing rather than substituting out-of-range results
- [ ] 7.9 **Test under-return**: a viewer restricted to a small channel subset requesting N results receives N — assert the *count*, since a membership-only assertion passes while the system silently under-returns
- [ ] 7.10 Build a golden set of ~20 questions over a seeded corpus and score recall@10 as the tuning harness for window size and RRF weights

## 8. MCP server

- [ ] 8.1 Implement the MCP server exposing `search_messages`, `thread_context`, and `list_channels`
- [ ] 8.2 Require viewer identity on every content-returning tool; error without it
- [ ] 8.3 **Authenticate the caller and derive viewer identity from the credential** — a client-supplied `viewer` parameter is an assertion, and trusting it over a network turns the whole ACL into an honour system
- [ ] 8.3a Implement tokens as a `token -> person` table: the bearer token *determines* the viewer and the request cannot name one. A leaked token then exposes one person's view, not everyone's
- [ ] 8.3b Store token hashes, never the tokens; support revoking and rotating a single person's token without disturbing others
- [ ] 8.4 Test: a caller authenticated for one person cannot retrieve as another
- [ ] 8.5 Make a channel the viewer cannot read indistinguishable from a channel that does not exist
- [ ] 8.6 Distinguish empty results from failures in the tool response shape
- [ ] 8.7 Test each tool's permission behaviour through the MCP layer, not only the repository layer
- [ ] 8.8 Document the `.mcp.json` stanza for connecting a Claude Code session to the server

## 9. Operations and governance

- [ ] 9.1 Implement a retention policy that purges messages and windows older than a configured window
- [ ] 9.2 Implement per-person opt-out that excludes their messages from ingestion and purges existing ones
- [ ] 9.3 Add structured logging and a health check covering gateway connectivity, backfill lag, and embedding backlog
- [ ] 9.4 Write the operator README: privileged-intent setup, indexing scope, retention, opt-out, and the DM limitation stated plainly
- [ ] 9.5 Add CI running `openspec validate --all --strict`, `ruff`, `mypy`, and `pytest`

## 10. Verification

- [ ] 10.1 `docker compose up`, run ingestion against a scratch guild containing a private channel
- [ ] 10.2 Connect a Claude Code session over MCP and confirm channel questions answer with working citation links
- [ ] 10.3 Confirm from a non-member viewer that the private channel is entirely invisible across all three tools
- [ ] 10.4 Delete a source message and confirm it disappears from results
- [ ] 10.5 Record backfill throughput and embedding cost per 10k messages to size a real deployment

## 11. Coolify deployment (Cyberdyne)

- [ ] 11.1 Write the production `Dockerfile` (uv, Python 3.11-slim, non-root user, no build secrets baked in)
- [ ] 11.2 Write `docker-compose.yml` for the Coolify `dockercompose` build pack with two services off one image: `ingest` (gateway + workers) and `mcp` (HTTP)
- [ ] 11.3 **Pin `ingest` to exactly one replica.** Two containers on one bot token both identify to the gateway and double-ingest every message; Discord will not error, the corpus just silently doubles
- [ ] 11.4 Add an HTTP health endpoint to *both* services — Coolify health checks are HTTP and the gateway process has no port of its own otherwise
- [ ] 11.5 Report gateway connectivity, backfill lag, and embedding backlog through the health endpoint so a crash-looping or silently-disconnected bot is visible
- [ ] 11.6 Run migrations as a deterministic pre-start step, idempotent across restarts and safe when two containers start together
- [ ] 11.7 Provision the Coolify-managed **pgvector** Postgres (not the stock `postgres:16` image, which lacks the extension) and enable scheduled backups
- [ ] 11.8 Set runtime env vars in Coolify: Discord token, database URL, OpenAI key, MCP bearer tokens — runtime scope, never build scope
- [ ] 11.9 Expose only the `mcp` service via FQDN; `ingest` gets no domain
- [ ] 11.10 Confirm the Cyberdyne box reaches `api.openai.com` before first deploy
- [ ] 11.11 Verify after deploy: gateway connected, a message posted in a scratch channel appears in the corpus, and an MCP query over HTTPS returns it with a working citation link
- [ ] 11.12 Verify a second `ingest` replica is genuinely prevented, not merely un-configured
