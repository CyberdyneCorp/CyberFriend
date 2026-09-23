"""`eth_call`, many `eth_call`s folded into one, and `eth_getLogs`. All reads.

The positions readers need hundreds of reads for a busy wallet. Sent as a
JSON-RPC batch, Infura's rate limiter rejected them; sent as one `eth_call` to
Multicall3's `aggregate3`, the same reads cost one request per chunk. So that
is the only shape this client offers besides the single call.

As in `rpc.py`: there is no signer and no `eth_sendRawTransaction` here, so
nothing built on this client can move funds whatever it is asked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from chatmemory.adapters.chain import abi

MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
"""Same address on every chain this package reads (deterministic deployment)."""

DEFAULT_CHUNK = 50
"""Calls per `aggregate3`. Infura accepted 134 on one key and refused 134 on
another; 50 was accepted by both, and a busy wallet is still a few requests."""


RATE_LIMIT_RETRIES = (0.5, 1.0, 2.0, 4.0)
"""Back-off, in seconds, after each "Too Many Requests". Reading three chains
at once bursts past Infura's per-second limit even on a key that handles the
volume; waiting a moment is enough, and failing the chain is not."""

RATE_LIMITED_CODE = -32005


class NodeError(RuntimeError):
    """The node answered with an error, or not in JSON-RPC at all."""


@dataclass(frozen=True, slots=True)
class Call:
    target: str
    data: str


class Node:
    """One chain's JSON-RPC endpoint, read-only."""

    def __init__(
        self,
        endpoint: str,
        client: httpx.AsyncClient,
        *,
        secret: str = "",
        chunk: int = DEFAULT_CHUNK,
        backoff: tuple[float, ...] = RATE_LIMIT_RETRIES,
    ) -> None:
        self._endpoint = endpoint
        self._client = client
        self._secret = secret
        self._chunk = max(1, chunk)
        self._backoff = backoff

    def redact(self, text: str) -> str:
        """The endpoint URL carries the key; keep it out of every message."""
        return text.replace(self._secret, "***") if self._secret else text

    async def eth_call(self, target: str, data: str, *, sender: str | None = None) -> bytes:
        params: dict[str, str] = {"to": target, "data": data}
        if sender is not None:
            # Only to *simulate* as the owner (v3 `collect`); an `eth_call`
            # changes no state whoever it claims to be from.
            params["from"] = sender
        result = await self._rpc("eth_call", [params, "latest"])
        return abi.to_bytes(str(result))

    async def block_number(self) -> int:
        return int(str(await self._rpc("eth_blockNumber", [])), 16)

    async def logs(
        self, address: str, topics: list[str | None], from_block: int, to_block: int
    ) -> list[dict[str, object]]:
        """`eth_getLogs` over a block range. Infura refuses ranges over 10,000."""
        query = {
            "address": address,
            "topics": topics,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        result = await self._rpc("eth_getLogs", [query])
        if not isinstance(result, list):
            return []
        return [entry for entry in result if isinstance(entry, dict)]

    async def _rpc(self, method: str, params: list[object]) -> object:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        response = await self._client.post(self._endpoint, json=body)
        for delay in self._backoff:
            if not _rate_limited(response):
                break
            await asyncio.sleep(delay)
            response = await self._client.post(self._endpoint, json=body)
        if response.status_code >= 400:
            raise NodeError(self.redact(f"HTTP {response.status_code}: {response.text[:200]}"))
        payload = response.json()
        if not isinstance(payload, dict) or "result" not in payload:
            error = payload.get("error") if isinstance(payload, dict) else payload
            raise NodeError(self.redact(f"{method} failed: {str(error)[:200]}"))
        return payload["result"]

    async def multicall(self, calls: list[Call]) -> list[bytes | None]:
        """Each call's return data in order, None where it reverted.

        Chunks run one after another, not concurrently: concurrency is what
        the rate limiter punishes, and the chunks are few.
        """
        out: list[bytes | None] = []
        for start in range(0, len(calls), self._chunk):
            chunk = calls[start : start + self._chunk]
            data = abi.aggregate3([(c.target, c.data) for c in chunk])
            raw = await self.eth_call(MULTICALL3, data)
            results = abi.decode_aggregate3(raw)
            if len(results) != len(chunk):
                raise NodeError("multicall returned a different number of results")
            out.extend(results)
        return out


def _rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    return isinstance(error, dict) and error.get("code") == RATE_LIMITED_CODE
