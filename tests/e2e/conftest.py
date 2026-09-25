"""End-to-end fixtures: a real Postgres, a sealed network, and the assembled bot.

The database is the real one, because the stores it exercises are where
production failures have hidden: memory provenance decides in SQL whether a
turn is kept, and the in-memory fakes re-implement those predicates. Locally
it is `TEST_DATABASE_URL` (the docker-compose pgvector, migrated by
`just migrate`), falling back to a throwaway testcontainers pgvector. With
`E2E_REQUIRE_DB=1` -- which CI sets -- an unavailable database fails the run
instead of skipping it, so this suite can never go silently green.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.process import NOW, e2e_settings, start
from tests.e2e.harness.web import NetworkSeal

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = "postgresql+asyncpg://chatmemory:chatmemory@localhost:5432/chatmemory"
PGVECTOR_IMAGE = "pgvector/pgvector:pg17"
# Raised from 60 s at 100 scenarios, when CI first ran 60.6 s with every test
# green. The budget exists to catch one slow scenario, not a growing suite.
BUDGET_SECONDS = 90.0

COLLEAGUE_CRYPTO = (
    "Our project treasury 0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5 holds the "
    "Uniswap liquidity for the launch; the position is in range and the token "
    "price is on the roadmap"
)
"""A colleague's message about *their* project: the known wrong answer to any
question about somebody's own wallet or positions."""

COFFEE = "The coffee maker in the kitchen will be fixed on Friday by the facilities team"
LEADERSHIP_SECRET = "Layoffs plan: the reorganisation cuts the platform team in October"

DEFAULT_CORPUS = (
    ("general", COLLEAGUE_CRYPTO, NOW - timedelta(days=2)),
    ("general", COFFEE, NOW - timedelta(days=1)),
    ("leadership", LEADERSHIP_SECRET, NOW - timedelta(hours=3)),
)


# --- the database -------------------------------------------------------


def _unavailable(reason: str) -> None:
    if os.environ.get("E2E_REQUIRE_DB") == "1":
        pytest.fail(f"E2E_REQUIRE_DB=1 and the end-to-end database is unavailable: {reason}")
    pytest.skip(f"no end-to-end database: {reason}")


async def _probe(url: str) -> str | None:
    """None when `url` is a migrated database, else what is wrong with it."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            found = await conn.scalar(text("SELECT to_regclass('public.conversation_turn')"))
    except Exception as exc:  # noqa: BLE001 - any failure means "not this one"
        return f"unreachable ({type(exc).__name__})"
    finally:
        await engine.dispose()
    return None if found else "not migrated; run `just migrate`"


def _migrate(url: str) -> None:
    env = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "LLM_API_KEY": "k", **os.environ}
    env["DATABASE_URL"] = url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True
    )


def _container() -> Any:
    """A throwaway pgvector, from whichever module this testcontainers has it in."""
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:
        from testcontainers.postgres import PostgresContainer
    return PostgresContainer(PGVECTOR_IMAGE, driver="asyncpg")


@pytest.fixture(scope="session")
def e2e_database_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL") or DEFAULT_URL
    problem = asyncio.run(_probe(url))
    if problem is None:
        yield url
        return
    if problem.startswith("not migrated"):
        _unavailable(f"{url}: {problem}")
    try:
        container = _container()
        container.start()
    except Exception as exc:  # noqa: BLE001 - no Docker is an environment problem
        _unavailable(f"{url} is {problem}, and testcontainers could not start: {exc}")
    try:
        container_url = container.get_connection_url()
        _migrate(container_url)
        yield container_url
    finally:
        container.stop()


@pytest_asyncio.fixture
async def clean(e2e_database_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine over an emptied database: every public table but alembic's."""
    engine = create_async_engine(e2e_database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text(
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
    try:
        yield engine
    finally:
        await engine.dispose()


# --- the network ----------------------------------------------------------


@pytest.fixture(autouse=True)
def sealed_network(monkeypatch: pytest.MonkeyPatch) -> NetworkSeal:
    """Every httpx client not built over `Edges.http_transport` fails loudly.

    The fake transport is an `httpx.MockTransport`, so it is untouched; httpx's
    real transports -- which the OpenAI SDK also sends through -- raise, and
    the seal records the host so `E2EBot.turn` fails even when a provider
    catches the error.
    """
    seal = NetworkSeal()

    async def refuse_async(self: Any, request: httpx.Request) -> httpx.Response:
        raise seal.refuse(request)

    def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise seal.refuse(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    return seal


# --- the bot ----------------------------------------------------------------


@pytest_asyncio.fixture
async def bot(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    """The production process over fake edges, in a guild with a seeded corpus."""
    e2e = await start(e2e_settings(e2e_database_url), clean, sealed_network)
    await e2e.seed_corpus(DEFAULT_CORPUS)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


# --- the time budget ----------------------------------------------------------

_spent = 0.0


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _spent
    if report.nodeid.startswith("tests/e2e/"):
        _spent += report.duration


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """The budget is enforced, not hoped for: over it, the run fails."""
    if _spent > BUDGET_SECONDS:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"end-to-end suite took {_spent:.1f}s, over its {BUDGET_SECONDS:.0f}s budget",
                red=True,
            )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
