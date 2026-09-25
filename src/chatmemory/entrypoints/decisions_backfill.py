"""Put history captured before the decision log back in the extraction queue.

    python -m chatmemory.entrypoints.decisions_backfill --since 2026-06-01 [--until 2026-09-01]

A one-off operator command, not a service. It resets the extraction watermark
of live, marker-bearing messages said on or after `--since` and, if given,
before `--until` (both midnight UTC) in the channels currently in indexing
scope, prints how many it reset, and exits. `--until` is the day decision
extraction was deployed: what the pass read after it already had its
decisions read, and resetting it would only pay for it again.
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
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chatmemory.adapters.store.config_postgres import PostgresConfigurationStore
from chatmemory.adapters.store.decisions_postgres import PostgresDecisionBackfill
from chatmemory.app.decisions.backfill import BackfillReport, DecisionBackfill
from chatmemory.app.scope import LiveScope
from chatmemory.config import Settings, get_settings
from chatmemory.ports.configuration import ConfigurationStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="decisions-backfill", description=__doc__)
    parser.add_argument(
        "--since",
        required=True,
        type=date.fromisoformat,
        help="first day to reset, YYYY-MM-DD, from midnight UTC",
    )
    parser.add_argument(
        "--until",
        type=date.fromisoformat,
        help="first day NOT to reset, YYYY-MM-DD, from midnight UTC: the day "
        "decision extraction was deployed (default: up to now)",
    )
    return parser


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.until is not None and args.until <= args.since:
        parser.error("--until must be a later day than --since")
    return args


def start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


async def backfill(
    engine: AsyncEngine,
    since: date,
    channel_ids: frozenset[int],
    until: date | None = None,
) -> BackfillReport:
    """The command's work, without the process around it, for tests to call."""
    return await DecisionBackfill(PostgresDecisionBackfill(engine)).run(
        start_of(since), channel_ids, start_of(until) if until is not None else None
    )


async def indexing_scope(
    configuration: ConfigurationStore, settings: Settings, environ: Mapping[str, str]
) -> frozenset[int]:
    """The channels in indexing scope as ingest reads them, or exit trying.

    Refused rather than run on the environment's scope, as ingest would at
    startup: that may still list a channel an operator removed, and a one-off
    can simply be run again.
    """
    scope = LiveScope.from_settings(configuration, settings, environ)
    if not (await scope.refresh()).applied:
        raise SystemExit("could not read stored configuration; nothing was reset")
    return scope.current()


def describe(report: BackfillReport, since: date, until: date | None = None) -> str:
    window = f"since {since.isoformat()}"
    if until is not None:
        window += f", before {until.isoformat()},"
    return (
        f"reset {report.reset} message(s) {window} for decision "
        f"extraction ({report.matched} carry a decision marker, "
        f"{report.already_pending} were already pending; {report.scanned} scanned)"
    )


async def run(argv: Sequence[str] | None = None) -> BackfillReport:
    args = _arguments(argv)
    settings = get_settings()
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        channels = await indexing_scope(
            PostgresConfigurationStore(engine), settings, os.environ
        )
        print(f"indexing scope: {len(channels)} channel(s)")
        report = await backfill(engine, args.since, channels, args.until)
    finally:
        await engine.dispose()
    print(describe(report, args.since, args.until))
    return report


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
