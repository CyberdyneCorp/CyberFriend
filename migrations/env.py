"""Alembic environment.

The database URL comes from application settings rather than alembic.ini so
that one source of configuration serves both the app and its migrations.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

config = context.config
target_metadata = None


def _url() -> str:
    """The database URL, read straight from the environment.

    Deliberately not via Settings: that validates every field, including the
    Discord credentials, which a migration has no use for. Going through it
    meant a first deploy -- where no bot token is configured yet -- failed on
    a validation error about `discord_guild_id` and took the deployment with
    it. A migration should need a database and nothing else.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set; nothing to migrate against")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)  # type: ignore[arg-type]
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _url())
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
