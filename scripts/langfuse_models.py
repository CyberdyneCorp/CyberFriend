"""Upsert model prices Langfuse does not know into Langfuse.

Usage (from the repository root, with the Langfuse keys in the environment):

    LANGFUSE_HOST=... LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... \
        uv run python scripts/langfuse_models.py [--dry-run] [--table PATH]

Reads `scripts/langfuse_model_prices.json` by default. Idempotent: a second run
reports every model `unchanged`. See docs/operations.md, "Model prices".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from chatmemory.adapters.tracing.langfuse_models import LangfuseModelSync, load_price_table

DEFAULT_TABLE = Path(__file__).with_name("langfuse_model_prices.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    args = parser.parse_args()
    missing = [
        name
        for name in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        print(f"missing environment: {', '.join(missing)}", file=sys.stderr)
        return 2
    sync = LangfuseModelSync(
        os.environ["LANGFUSE_HOST"],
        os.environ["LANGFUSE_PUBLIC_KEY"],
        os.environ["LANGFUSE_SECRET_KEY"],
    )
    results = asyncio.run(sync.sync(load_price_table(args.table), dry_run=args.dry_run))
    for model, result in results.items():
        print(f"{model}: {result}{' (dry run)' if args.dry_run and result != 'unchanged' else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
