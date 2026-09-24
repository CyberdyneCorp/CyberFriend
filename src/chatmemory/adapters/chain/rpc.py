"""A JSON-RPC client with two verbs and no way to acquire a third.

`eth_getBalance` and `eth_call` are the whole surface. There is no signer, no
private key and no mnemonic in this package, so there is nothing here that
could sign a transaction even if some later caller asked it to. That is the
difference between a component that is read-only and one that currently only
reads.

Failures are values, not exceptions. A chain that cannot be reached has to be
reported as unreachable rather than as an address holding nothing -- once
those two become prose they are indistinguishable, and only one of them is a
reason to worry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import httpx
import structlog

from chatmemory.adapters.chain.node import RATE_LIMIT_RETRIES, rate_limited
from chatmemory.adapters.chain.tokens import Chain, Token
from chatmemory.domain.chain import normalise

log = structlog.get_logger()

DEFAULT_TIMEOUT = 8.0

BALANCE_OF = "0x70a08231"
"""`balanceOf(address)`. The only function selector this package knows."""


@dataclass(frozen=True, slots=True)
class Holding:
    symbol: str
    amount: Decimal
    dollar_pegged: bool = False


@dataclass(frozen=True, slots=True)
class ChainBalances:
    """What one chain reported, or why it did not."""

    chain: Chain
    native: Decimal | None = None
    tokens: tuple[Holding, ...] = ()
    unreachable: str = ""

    @property
    def ok(self) -> bool:
        return not self.unreachable


def _call_data(address: str) -> str:
    """`balanceOf` with the address left-padded into a 32-byte word."""
    return BALANCE_OF + normalise(address)[2:].rjust(64, "0")


def _units(hex_value: str, decimals: int) -> Decimal:
    return Decimal(int(hex_value, 16)) / (Decimal(10) ** decimals)


class ChainReader:
    """Reads balances for one chain over Infura."""

    def __init__(
        self,
        chain: Chain,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        endpoint: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        backoff: tuple[float, ...] = RATE_LIMIT_RETRIES,
    ) -> None:
        self.chain = chain
        self._backoff = backoff
        self._endpoint = endpoint or f"https://{chain.infura_host}.infura.io/v3/{api_key}"
        self._client = client
        self._timeout = timeout_seconds
        self._transport = transport
        # The key is in the URL, and httpx puts the full URL into the text of
        # an HTTPStatusError. One unhandled 401 would otherwise write it into
        # the log, so it is scrubbed from every message this class emits.
        self._secret = api_key

    def redact(self, text: str) -> str:
        return text.replace(self._secret, "***") if self._secret else text

    async def balances(self, address: str, tokens: tuple[Token, ...]) -> ChainBalances:
        """Native and token balances, or an unreachable report. Never raises."""
        try:
            return await asyncio.wait_for(
                self._read(address, tokens), timeout=self._timeout
            )
        except TimeoutError:
            log.warning("chain.timeout", chain=self.chain.key, timeout=self._timeout)
            return ChainBalances(self.chain, unreachable="timed out")
        except Exception as exc:  # noqa: BLE001 - a chain must never fail the run
            log.warning("chain.failed", chain=self.chain.key, error=self.redact(str(exc)))
            return ChainBalances(self.chain, unreachable="could not be reached")

    async def _read(self, address: str, tokens: tuple[Token, ...]) -> ChainBalances:
        if self._client is not None:
            return await self._read_with(self._client, address, tokens)
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            return await self._read_with(client, address, tokens)

    async def _read_with(
        self, client: httpx.AsyncClient, address: str, tokens: tuple[Token, ...]
    ) -> ChainBalances:
        who = normalise(address)
        # One batch rather than a request per token: a wallet with four known
        # tokens is five round trips otherwise, and the timeout above is for
        # the whole chain.
        batch = [
            {"jsonrpc": "2.0", "id": 0, "method": "eth_getBalance", "params": [who, "latest"]},
            *(
                {
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": "eth_call",
                    "params": [{"to": t.contract, "data": _call_data(who)}, "latest"],
                }
                for i, t in enumerate(tokens, start=1)
            ),
        ]
        response = await client.post(self._endpoint, json=batch, timeout=self._timeout)
        # The same back-off as `Node`. Without it a balance read straight
        # after a positions read was reported "could not be reached" on
        # Arbitrum, and a single retry a moment later answered.
        for delay in self._backoff:
            if not rate_limited(response):
                break
            await asyncio.sleep(delay)
            response = await client.post(self._endpoint, json=batch, timeout=self._timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            # A single error object comes back unbatched.
            raise ValueError(f"unexpected response: {str(payload)[:120]}")

        by_id = {r.get("id"): r for r in payload if isinstance(r, dict)}
        native_raw = by_id.get(0, {}).get("result")
        if not isinstance(native_raw, str):
            raise ValueError("no native balance in response")

        holdings: list[Holding] = []
        for i, token in enumerate(tokens, start=1):
            raw = by_id.get(i, {}).get("result")
            # A token that did not answer is skipped rather than reported as
            # zero: "the contract did not reply" is not "you hold none".
            if not isinstance(raw, str) or raw in ("", "0x"):
                continue
            amount = _units(raw, token.decimals)
            if amount > 0:
                holdings.append(Holding(token.symbol, amount, token.dollar_pegged))

        return ChainBalances(
            chain=self.chain,
            native=_units(native_raw, 18),
            tokens=tuple(holdings),
        )
