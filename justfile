# CyberFriend
#
# `just` with no arguments lists every recipe.
#
# The database is the one thing most recipes need. `just up` starts it;
# `just db-reset` is the way back when a migration or a test leaves it odd.

set dotenv-load := true

venv := ".venv"
py := venv / "bin/python"
pytest := venv / "bin/pytest"
dev_db := "postgresql+asyncpg://chatmemory:chatmemory@localhost:5432/chatmemory"

# Placeholders for the recipes that need settings to load but never use them:
# Settings refuses to construct without these, and a lint run should not need
# a Discord token.
export DISCORD_TOKEN := env_var_or_default("DISCORD_TOKEN", "x")
export DISCORD_GUILD_ID := env_var_or_default("DISCORD_GUILD_ID", "1")
export LLM_API_KEY := env_var_or_default("LLM_API_KEY", "k")
export DATABASE_URL := env_var_or_default("DATABASE_URL", dev_db)
# Integration tests read this one, and an empty value sends them at the
# default database, so it is set explicitly rather than inherited.
export TEST_DATABASE_URL := env_var_or_default("TEST_DATABASE_URL", dev_db)

default:
    @just --list --unsorted

# --- setup ---------------------------------------------------------------

# Create the virtualenv and install the project with its dev extras.
install:
    uv venv
    uv pip install -e '.[dev]'
    cd console && npm ci

# Start Postgres with pgvector, and wait until it answers.
up:
    docker compose -f docker-compose.dev.yml up -d
    @until docker compose -f docker-compose.dev.yml exec -T db pg_isready -U chatmemory >/dev/null 2>&1; do sleep 1; done
    @echo "postgres ready"

down:
    docker compose -f docker-compose.dev.yml down

# Apply every migration to the development database.
migrate: up
    {{ venv }}/bin/alembic upgrade head

# The way back from a half-applied migration or a test that left rows behind.
# Drops and rebuilds the development database.
db-reset: up
    -docker compose -f docker-compose.dev.yml exec -T db psql -U chatmemory -d postgres -c "DROP DATABASE IF EXISTS chatmemory WITH (FORCE)"
    docker compose -f docker-compose.dev.yml exec -T db psql -U chatmemory -d postgres -c "CREATE DATABASE chatmemory"
    @just migrate

# A psql shell on the development database.
psql:
    docker compose -f docker-compose.dev.yml exec -it db psql -U chatmemory -d chatmemory

# --- checks --------------------------------------------------------------

# Everything CI runs, in the order that fails fastest.
check: lint types test spec

lint:
    {{ venv }}/bin/ruff check src/ tests/

fmt:
    {{ venv }}/bin/ruff check --fix src/ tests/
    {{ venv }}/bin/ruff format src/ tests/

types:
    {{ venv }}/bin/mypy

spec:
    openspec validate --all --strict

# Unit tests only: no database, a few seconds.
test-unit:
    {{ pytest }} tests/unit -q

# Unit and integration. Needs the database, so it migrates first.
test: migrate
    {{ pytest }} tests -q

# One file, or one test: `just t tests/unit/test_memory.py -k recall`
t *ARGS: migrate
    {{ pytest }} {{ ARGS }}

# Spends embedding calls against the real endpoint; see tests/evaluation/BASELINE.md.
# The retrieval golden set.
goldens:
    #!/usr/bin/env bash
    set -euo pipefail
    just up
    docker compose -f docker-compose.dev.yml exec -T db psql -U chatmemory -d postgres \
      -c "CREATE DATABASE chatmemory_goldens OWNER chatmemory" 2>/dev/null || true
    DATABASE_URL='postgresql+asyncpg://chatmemory:chatmemory@localhost:5432/chatmemory_goldens' \
      {{ venv }}/bin/alembic upgrade head
    DATABASE_URL='postgresql+asyncpg://chatmemory:chatmemory@localhost:5432/chatmemory_goldens' \
    TEST_DATABASE_URL='postgresql+asyncpg://chatmemory:chatmemory@localhost:5432/chatmemory_goldens' \
      {{ pytest }} tests/evaluation -m goldens -s

# --- running -------------------------------------------------------------

# Ingest: capture, backfill, windowing, embeddings, ask extraction.
run-ingest: migrate
    {{ py }} -m chatmemory.entrypoints.ingest

# Answers questions in Discord.
run-bot: migrate
    {{ py }} -m chatmemory.entrypoints.bot

# The MCP interface to the corpus.
run-mcp: migrate
    {{ py }} -m chatmemory.entrypoints.mcp_server

# The operator console API, which also serves the built console.
run-admin: migrate
    {{ py }} -m chatmemory.entrypoints.admin

# The console with hot reload, against a locally running admin API.
run-console:
    cd console && npm run dev

# --- console -------------------------------------------------------------

console-build:
    cd console && npm run build

console-test:
    cd console && npm run test

# --- images --------------------------------------------------------------

# Build the production image, exactly as the platform builds it.
build:
    docker build -t cyberfriend:local .

# Run the whole stack from the production compose file. Needs a filled .env.
compose-up:
    docker compose up -d --build

compose-down:
    docker compose down

# --- operator ------------------------------------------------------------

# One credential per person: the name is what their changes are attributed to.
# Issue a console credential.
admin-token OPERATOR LABEL="laptop":
    {{ py }} -m chatmemory.admin.issue_token issue {{ OPERATOR }} --label {{ LABEL }}

admin-tokens:
    {{ py }} -m chatmemory.admin.issue_token list

# Issue an MCP credential for one Discord account.
mcp-token DISCORD_USER_ID LABEL="laptop":
    {{ py }} -m chatmemory.mcp.issue_token issue {{ DISCORD_USER_ID }} --label {{ LABEL }}

# Opting a person out is done from the admin console (/api/optouts); there is
# no CLI for it.

# --- specs ---------------------------------------------------------------

specs:
    openspec list

# Progress of one change: `just spec-status add-ask-extraction`
spec-status CHANGE:
    openspec status --change {{ CHANGE }}
