"""Integration fixtures.

These need a live pgvector Postgres (docker compose -f docker-compose.dev.yml
up -d). They are skipped, not failed, when one is unreachable -- a missing
database is an environment problem, not a defect in the code under test.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Optional: a key file outside the repo, so the secret is never copied in.
# Point OPENAI_KEY_FILE elsewhere, or just export OPENAI_API_KEY.
_KEY_FILE = os.environ.get(
    "OPENAI_KEY_FILE",
    "/Users/leonardoaraujo/work/ingestion-knowledge-graph/openai_key.env",
)


def _load_key_file() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    try:
        for line in pathlib.Path(_KEY_FILE).read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "OPENAI_API_KEY" and value.strip():
                os.environ["OPENAI_API_KEY"] = value.strip().strip("\"'")
    except OSError:
        pass


_load_key_file()

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://chatmemory:chatmemory@localhost:5432/chatmemory",
)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(DB_URL, poolclass=None)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await eng.dispose()
        pytest.skip(f"no test database available: {type(exc).__name__}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def clean(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                # Derived from the catalog rather than listed by hand. The
                # hand-written list covered 8 of 19 tables and silently
                # stopped covering each new one, so state leaked between
                # tests and surfaced as an unrelated unique-violation in
                # whichever test happened to run second.
                """
                DO $$
                DECLARE tables text;
                BEGIN
                    SELECT string_agg(format('%I.%I', schemaname, tablename), ', ')
                      INTO tables
                      FROM pg_tables
                     WHERE schemaname = 'public'
                       AND tablename <> 'alembic_version';
                    IF tables IS NOT NULL THEN
                        EXECUTE 'TRUNCATE ' || tables || ' RESTART IDENTITY CASCADE';
                    END IF;
                END $$;
                """
            )
        )
    yield engine


@pytest.fixture
def openai_key() -> str:
    """Skip tests that would spend money when no key is configured."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("no OPENAI_API_KEY configured")
    return key
