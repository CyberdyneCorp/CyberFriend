"""Model price definitions in Langfuse, from a checked-in table.

Langfuse prices a generation from its `model` and `usageDetails` using its own
price table. It ships prices for gpt-4o and text-embedding-3-small, not for the
self-hosted AminiLLM models or `gpt-4o-mini-transcribe`, so those are
upserted here through `/api/public/models`.

An ops step, run by hand (`scripts/langfuse_models.py`), never at startup:
idempotent, so running it twice changes nothing the second time. Langfuse has
no update for a model definition, so a changed price replaces ours: our
definitions with that name are deleted and one is created. Definitions
Langfuse manages itself are never touched.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_TIMEOUT = 10.0
PAGE_SIZE = 100
PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """One row of the price table: USD per token, as Langfuse stores it."""

    model_name: str
    match_pattern: str
    input_price: float
    output_price: float


def load_price_table(path: Path) -> list[ModelPrice]:
    """The checked-in table, whose prices are written per million tokens."""
    raw = json.loads(path.read_text())
    return [
        ModelPrice(
            model_name=str(row["model_name"]),
            match_pattern=str(row["match_pattern"]),
            input_price=float(row["input_per_million"]) / PER_MILLION,
            output_price=float(row["output_per_million"]) / PER_MILLION,
        )
        for row in raw["models"]
    ]


class LangfuseModelSync:
    """Brings Langfuse's user-defined model prices in line with a table."""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = host.rstrip("/") + "/api/public/models"
        self._auth = (public_key, secret_key)
        self._timeout = timeout
        self._transport = transport

    async def sync(
        self, prices: Sequence[ModelPrice], dry_run: bool = False
    ) -> dict[str, str]:
        """Upsert each price; returns `created`, `updated` or `unchanged` per model."""
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport, auth=self._auth
        ) as client:
            existing = await self._ours(client)
            return {
                price.model_name: await self._upsert(
                    client, price, existing.get(price.model_name, []), dry_run
                )
                for price in prices
            }

    async def _upsert(
        self,
        client: httpx.AsyncClient,
        price: ModelPrice,
        current: list[dict[str, Any]],
        dry_run: bool,
    ) -> str:
        if len(current) == 1 and _same(current[0], price):
            return "unchanged"
        if not dry_run:
            for row in current:
                response = await client.delete(f"{self._url}/{row['id']}")
                response.raise_for_status()
            response = await client.post(self._url, json=_body(price))
            response.raise_for_status()
        return "updated" if current else "created"

    async def _ours(self, client: httpx.AsyncClient) -> dict[str, list[dict[str, Any]]]:
        """Our definitions by name, across every page; Langfuse's own left out."""
        found: dict[str, list[dict[str, Any]]] = {}
        page = 1
        while True:
            response = await client.get(self._url, params={"page": page, "limit": PAGE_SIZE})
            response.raise_for_status()
            body = response.json()
            rows = body.get("data") or []
            for row in rows:
                if not row.get("isLangfuseManaged"):
                    found.setdefault(str(row.get("modelName")), []).append(row)
            total_pages = int((body.get("meta") or {}).get("totalPages") or 0)
            if not rows or page >= total_pages:
                return found
            page += 1


def _body(price: ModelPrice) -> dict[str, Any]:
    return {
        "modelName": price.model_name,
        "matchPattern": price.match_pattern,
        "unit": "TOKENS",
        "inputPrice": price.input_price,
        "outputPrice": price.output_price,
    }


def _same(row: dict[str, Any], price: ModelPrice) -> bool:
    return (
        row.get("matchPattern") == price.match_pattern
        and _price(row.get("inputPrice")) == price.input_price
        and _price(row.get("outputPrice")) == price.output_price
    )


def _price(value: object) -> float | None:
    # Langfuse returns prices as numbers or decimal strings, depending on version.
    if value is None:
        return None
    return float(str(value))
