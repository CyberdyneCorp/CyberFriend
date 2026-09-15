"""Apply database migrations, then exit.

Runs as a one-shot service that the long-running services wait on, so the
schema is in place before anything queries it. Without this the containers
start against an empty database and fail on their first statement -- which
looks like an application bug and is not one.

Only this service migrates. Letting each service run migrations on start
would have three containers racing the same DDL on every deploy; alembic
takes no lock of its own, so that race is real rather than theoretical.
"""

from __future__ import annotations

import asyncio
import os
import sys

import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from chatmemory import logging as log_setup

log = structlog.get_logger()

CONNECT_ATTEMPTS = 30
CONNECT_DELAY_SECONDS = 2.0


def database_url() -> str:
    """Read DATABASE_URL directly, not through Settings.

    Settings validates every field, including the Discord credentials --
    which a schema migration has no use for. Loading it here coupled
    migrating the database to having a bot token: on a first deploy, where
    the token is not yet configured, the migrate job died on a validation
    error about `discord_guild_id` and took the whole deployment with it.
    A job should require what it uses and nothing else.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set; nothing to migrate against")
    return url


async def wait_for_database(url: str) -> None:
    """Block until Postgres answers.

    The database is a separate Coolify resource, not a compose service, so
    there is no healthcheck to depend on -- a deploy that restarts both will
    routinely start this before Postgres is accepting connections.
    """
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                log.info("migrate.database_reachable", attempt=attempt)
                return
            except Exception as exc:  # noqa: BLE001 - any failure means wait
                if attempt == CONNECT_ATTEMPTS:
                    log.error("migrate.database_unreachable", error=type(exc).__name__)
                    raise
                await asyncio.sleep(CONNECT_DELAY_SECONDS)
    finally:
        await engine.dispose()


def main() -> int:
    log_setup.configure()
    url = database_url()

    asyncio.run(wait_for_database(url))

    config = Config("alembic.ini")
    log.info("migrate.upgrading")
    # Idempotent: already at head is a no-op, so restarts are free.
    command.upgrade(config, "head")
    log.info("migrate.complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
