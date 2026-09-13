"""Integration fixtures.

These need a live pgvector Postgres (docker compose -f docker-compose.dev.yml
up -d). They are skipped, not failed, when one is unreachable -- a missing
database is an environment problem, not a defect in the code under test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
                "TRUNCATE conversation_window_message, conversation_window, "
                "message_mention, message, ingest_cursor, channel, "
                "person_platform_id, person RESTART IDENTITY CASCADE"
            )
        )
    yield engine
