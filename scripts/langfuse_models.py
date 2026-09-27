"""Register model prices in Langfuse: the checked-in table and every configured model.

Usage (from the repository root, with the Langfuse keys in the environment and
the model settings as the deployment has them):

    LANGFUSE_HOST=... LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... \\
    CHAT_MODEL=... EXTRACTION_MODEL=... EMBEDDING_MODEL=... MEDIA_AUDIO_MODEL=... \\
        uv run python scripts/langfuse_models.py [--dry-run] [--table PATH] \\
            [--price NAME=INPUT,OUTPUT ...]

Reads `scripts/langfuse_model_prices.json` by default. Each configured model
(`CHAT_MODEL`, `EXTRACTION_MODEL`, `EMBEDDING_MODEL`, `MEDIA_AUDIO_MODEL`,
defaults when unset) must be priced by the table, by a `--price` (USD per
million input and output tokens), or by Langfuse itself; otherwise the script
names the missing models and writes nothing. Prices are never guessed.
Idempotent: a second run reports every model `unchanged`. See
docs/operations.md, "Model prices".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx

from chatmemory.adapters.tracing.langfuse_models import (
    MODEL_SETTINGS,
    LangfuseModelSync,
    ModelPrice,
    configured_models,
    load_price_table,
    plan_prices,
    price_from_flag,
)
from chatmemory.config import Settings

DEFAULT_TABLE = Path(__file__).with_name("langfuse_model_prices.json")
KEYS = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


def model_defaults() -> dict[str, str]:
    """Each model setting's default, from `Settings` without instantiating it."""
    return {
        var: str(Settings.model_fields[field].get_default())
        for var, field in MODEL_SETTINGS.items()
    }


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="NAME=INPUT,OUTPUT",
        help="price for a model the table and Langfuse do not know, USD per 1M tokens",
    )
    return parser.parse_args(argv)


async def run(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    host, public_key, secret_key = (environ[name] for name in KEYS)
    sync = LangfuseModelSync(host, public_key, secret_key, transport=transport)
    try:
        flags = [price_from_flag(raw) for raw in args.price]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    configured = configured_models(environ, model_defaults())
    prices, missing = plan_prices(
        configured, load_price_table(args.table), flags, await sync.managed_patterns()
    )
    print(f"configured models: {', '.join(configured)}")
    if missing:
        _report_missing(missing)
        return 2
    results = await sync.sync(prices, dry_run=args.dry_run)
    _report(results, prices, flags, configured, args.dry_run)
    return 0


def _report_missing(missing: list[str]) -> None:
    print(
        "no price for: " + ", ".join(missing) + ". Langfuse does not price them and "
        "the table does not list them. Give each one with "
        "--price NAME=INPUT,OUTPUT (USD per million tokens, from the provider's "
        "price list), or add it to the table.",
        file=sys.stderr,
    )


def _report(
    results: dict[str, str],
    prices: list[ModelPrice],
    flags: list[ModelPrice],
    configured: list[str],
    dry_run: bool,
) -> None:
    for model, result in results.items():
        print(f"{model}: {result}{' (dry run)' if dry_run and result != 'unchanged' else ''}")
    ours = {price.model_name for price in prices}
    for model in configured:
        if model not in ours:
            print(f"{model}: priced by Langfuse or matched by a table row")
    if flags:
        print("add the --price models to the table so the next run keeps them")


def main() -> int:
    args = arguments()
    missing = [name for name in KEYS if not os.environ.get(name)]
    if missing:
        print(f"missing environment: {', '.join(missing)}", file=sys.stderr)
        return 2
    return asyncio.run(run(args, os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
