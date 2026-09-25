"""Put history captured before the decision log back in the extraction queue.

    python -m chatmemory.entrypoints.decisions_backfill --since 2026-06-01

A one-off operator command, not a service. It resets the extraction watermark
of live, marker-bearing messages said on or after `--since` (midnight UTC) in
the channels currently in indexing scope, prints how many it reset, and exits.
The ingest process's backlog worker then re-extracts them at its usual rate
bound; see `docs/operations.md` for the cost, and for what re-extraction does
to the asks already recorded on those messages.

Scope is read the way ingest reads it -- stored configuration, then the
environment -- so the command resets exactly what the worker will go on to
read, and nothing in a channel an operator removed. If stored configuration
cannot be read the command stops without resetting anything.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime, time

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.adapters.store.decisions_postgres import PostgresDecisionBackfill
from chatmemory.app.decisions.backfill import BackfillReport, DecisionBackfill
from chatmemory.app.scope import LiveScope
from chatmemory.config import get_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="decisions-backfill", description=__doc__)
    parser.add_argument(
        "--since",
        required=True,
        type=date.fromisoformat,
        help="first day to reset, YYYY-MM-DD, from midnight UTC",
    )
    return parser


def start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


async def backfill(engine: AsyncEngine, since: date, channel_ids: frozenset[int]) -> BackfillReport:
    """The command's work, without the process around it, for tests to call."""
    return await DecisionBackfill(PostgresDecisionBackfill(engine)).run(
        start_of(since), channel_ids
    )


def describe(report: BackfillReport, since: date) -> str:
    return (
        f"reset {report.reset} message(s) since {since.isoformat()} for decision "
        f"extraction ({report.matched} carry a decision marker, "
        f"{report.already_pending} were already pending; {report.scanned} scanned)"
    )


async def run(argv: Sequence[str] | None = None) -> BackfillReport:
    args = _parser().parse_args(argv)
    settings = get_settings()
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        scope = LiveScope.from_settings(PostgresConfigurationStore(engine), settings, os.environ)
        # Refused rather than run on the environment's scope, as ingest would
        # at startup: that may still list a channel an operator removed, and
        # a one-off can simply be run again.
        if not (await scope.refresh()).applied:
            raise SystemExit("could not read stored configuration; nothing was reset")
        channels = scope.current()
        print(f"indexing scope: {len(channels)} channel(s)")
        report = await backfill(engine, args.since, channels)
    finally:
        await engine.dispose()
    print(describe(report, args.since))
    return report


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
