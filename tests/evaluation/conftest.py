"""Fixtures for the retrieval golden set.

Two decisions live here and both are deliberate.

**It does not run in the ordinary suite.** Every question is asked as every
viewer against the real embedding endpoint, so a run costs money and about a
minute. `pytest` must therefore not pick it up by accident; it is opted into,
by marker or by environment variable, and the marker is registered in
`pyproject.toml` so the opt-in is discoverable rather than folklore.

**When it does run, a missing dependency fails.** The integration suite skips
when the database or the key is absent, which is right for a check that runs
constantly: a missing database is an environment problem rather than a defect.
This one is different. Somebody asked for these numbers, and a skipped
measurement reported as a pass is the one outcome worse than no measurement at
all -- so an unreachable database or an absent key stops the run loudly.

The sweep fixture is synchronous and drives its own event loop. Seeding,
embedding and every search happen inside one `asyncio.run`, and what comes back
is plain data, so the assertions are ordinary synchronous functions and none of
them can accidentally issue a second query against a loop that has closed.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Imported for its side effect as much as for the URL: it loads the OpenAI key
# from the same out-of-repo file the integration suite uses, so there is one
# place that knows where the secret lives.
from tests.evaluation.harness import Sweep, seed, sweep
from tests.integration.conftest import DB_URL

MARKER = "goldens"
ENV_FLAG = "RUN_RETRIEVAL_GOLDENS"

GOLDENS_DATABASE = "chatmemory_goldens"
"""A database of its own, and not the development one.

Seeding starts by truncating every table, because a golden set measured
against whatever happened to be lying around is not measuring the corpus it
describes. Pointing that at the shared development database would destroy
whatever anyone else was working on -- which is exactly what happened the
first time this ran. Override with GOLDENS_DATABASE_URL.
"""


def _goldens_url() -> str:
    """The configured URL, or the dev one with its database name swapped.

    Swapped rather than hardcoded so host, port and credentials keep coming
    from the one place the rest of the suite reads them from.

    Rendered with `hide_password=False` because `str()` on a URL masks it --
    which silently produced a connection string authenticating as `***` and a
    failure that read like a wrong password rather than a wrong call.
    """
    configured = os.environ.get("GOLDENS_DATABASE_URL")
    if configured:
        return configured
    return make_url(DB_URL).set(database=GOLDENS_DATABASE).render_as_string(
        hide_password=False
    )


GOLDENS_URL = _goldens_url()
GOLDENS_URL_SHOWN = str(make_url(GOLDENS_URL))
"""The same URL with the password masked. Everything printed uses this one."""

SETUP = (
    "  docker compose -f docker-compose.dev.yml up -d\n"
    f'  docker exec <db> psql -U chatmemory -d postgres -c "CREATE DATABASE {GOLDENS_DATABASE};"\n'
    f"  DATABASE_URL={GOLDENS_URL_SHOWN} .venv/bin/alembic upgrade head"
)

HOW_TO_RUN = (
    "the retrieval golden set is opt-in because it spends embedding calls; run it with\n"
    f"{SETUP}\n"
    f"  .venv/bin/pytest tests/evaluation -m {MARKER} -s\n"
    f"or set {ENV_FLAG}=1. See tests/evaluation/BASELINE.md."
)


def _requested(config: pytest.Config) -> bool:
    """Whether this run asked for the golden set.

    The marker expression is matched rather than compared so that
    `-m "goldens and not slow"` counts, while the far more common
    `-m "not goldens"` does not.
    """
    flag = os.environ.get(ENV_FLAG, "").strip().lower()
    if flag not in {"", "0", "false", "no"}:
        return True
    markexpr = str(getattr(config.option, "markexpr", "") or "")
    return bool(re.search(rf"(?<!not )\b{MARKER}\b", markexpr))


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if _requested(config):
        return
    skipped = pytest.mark.skip(reason=HOW_TO_RUN)
    for item in items:
        if MARKER in item.keywords:
            item.add_marker(skipped)


@pytest.fixture(scope="module")
def api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise AssertionError(
            "the golden set measures real retrieval and needs OPENAI_API_KEY; "
            "a skipped measurement is not a measurement"
        )
    return key


@pytest.fixture(scope="module")
def measured(api_key: str) -> Iterator[Sweep]:
    """One seed and one full sweep, shared by every assertion in the module."""
    result = asyncio.run(_seed_and_sweep(api_key))
    print("\n" + result.report.render())  # noqa: T201 - this file exists to report
    yield result


async def _seed_and_sweep(key: str) -> Sweep:
    engine = create_async_engine(GOLDENS_URL, poolclass=None)
    try:
        await _require_database(engine)
        seeded = await seed(engine, key)
        return await sweep(engine, seeded, key)
    finally:
        await engine.dispose()


async def _require_database(engine: AsyncEngine) -> None:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the message
        raise AssertionError(
            f"the golden set needs its own pgvector database at {GOLDENS_URL_SHOWN}: "
            f"{type(exc).__name__}: {exc}\n"
            f"create it with:\n{SETUP}"
        ) from exc
