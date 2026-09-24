"""A wallet's recent rows from a Blockscout explorer, read-only and keyless.

`/api/v2/advanced-filters`, not `/addresses/{a}/transactions`. The address
list holds only transactions the wallet itself sent, and an EIP-7702 smart
account's actions are usually sent by a relayer: an Aave supply and an LP
withdrawal on a real wallet were missing from it entirely. Advanced filters
return one list of top-level transactions, internal value transfers and token
transfers in or out of the address, filtered by time on the server.

Its cursor has a trap. `next_page_params` comes back with some fields null
(`internal_transaction_index`, usually): leaving them out returns the first
page again, forever, and sending "None" is an HTTP 422. An empty string is
what the server accepts, so a null is sent as one -- and a cursor that repeats
anyway ends the read rather than looping.

The internal-transactions endpoint is not used: it took 44 s or timed out on
the same wallets, and advanced filters already carry internal value moves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

MAX_PAGES = 4
"""Fifty rows a page. The busiest week in twenty months of a real wallet was
71 rows; four pages is a month of that, and hitting the cap is said, never
silently cut."""
ROWS_PER_PAGE = 50
MAX_ROWS = MAX_PAGES * ROWS_PER_PAGE

Row = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Rows:
    items: tuple[Row, ...]
    #: More rows existed than `MAX_PAGES` pages hold.
    truncated: bool = False
    #: The newest block the explorer has indexed, when it said.
    indexed_until: datetime | None = None


def next_cursor(following: Mapping[str, object]) -> dict[str, str]:
    """`next_page_params` as query parameters, a null sent as an empty string."""
    return {key: "" if value is None else str(value) for key, value in following.items()}


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


async def activity_rows(
    client: httpx.AsyncClient,
    explorer: str,
    address: str,
    start: datetime,
    end: datetime,
    *,
    max_pages: int = MAX_PAGES,
) -> Rows:
    """Every row in or out of `address` between `start` and `end`, newest first."""
    base = {
        "from_address_hashes_to_include": address,
        "to_address_hashes_to_include": address,
        "address_relation": "or",
        "age_from": _stamp(start),
        "age_to": _stamp(end),
    }
    params = dict(base)
    items: list[Row] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for _ in range(max_pages):
        response = await client.get(f"{explorer}/api/v2/advanced-filters", params=params)
        response.raise_for_status()
        body = response.json()
        items.extend(body.get("items") or [])
        following = body.get("next_page_params")
        if not following:
            return Rows(tuple(items))
        cursor = next_cursor(following)
        key = tuple(sorted(cursor.items()))
        if key in seen:
            # The same page again: the loop the null-field trap causes.
            return Rows(tuple(items), truncated=True)
        seen.add(key)
        params = {**base, **cursor}
    return Rows(tuple(items), truncated=True)


async def indexed_until(client: httpx.AsyncClient, explorer: str) -> datetime | None:
    """When the newest block the explorer has indexed was made, or None.

    An explorer can stop indexing and still report "finished": Arbitrum's was
    two days behind the chain while claiming it, so a position minted in
    those two days read as "no activity". Asked once per lookup, never
    trusted to be current.
    """
    try:
        response = await client.get(f"{explorer}/api/v2/blocks", params={"type": "block"})
        response.raise_for_status()
        newest = (response.json().get("items") or [{}])[0].get("timestamp")
        return datetime.fromisoformat(str(newest).replace("Z", "+00:00")) if newest else None
    except Exception:  # noqa: BLE001 - unknown freshness is reported as nothing
        return None


MULTICALL = "0xac9650d8"
"""`multicall(bytes[])`, how the Uniswap v3 position manager is usually called."""


async def multicall_selectors(
    client: httpx.AsyncClient, explorer: str, address: str
) -> dict[str, frozenset[str]]:
    """The inner selectors of the wallet's recent multicalls, by transaction hash.

    Only the address list carries `decoded_input`, and only for transactions
    the wallet sent, so this is one page of it, read only when a position
    manager multicall is in the rows. What it tells apart -- decrease plus
    collect, or collect alone -- the token flows cannot.
    """
    response = await client.get(
        f"{explorer}/api/v2/addresses/{address}/transactions", params={"filter": "from"}
    )
    response.raise_for_status()
    found: dict[str, frozenset[str]] = {}
    for item in response.json().get("items") or []:
        inner = _inner_calls(item.get("decoded_input"))
        if inner:
            found[str(item.get("hash", "")).lower()] = frozenset(c[:10].lower() for c in inner)
    return found


def _inner_calls(decoded: object) -> Sequence[str]:
    if not isinstance(decoded, Mapping) or decoded.get("method_id") != MULTICALL[2:]:
        return ()
    for parameter in decoded.get("parameters") or []:
        value = parameter.get("value") if isinstance(parameter, Mapping) else None
        if isinstance(value, list):
            return [v for v in value if isinstance(v, str)]
    return ()
