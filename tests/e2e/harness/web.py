"""Outbound HTTP, answered by host.

One `httpx.MockTransport` is handed to the graph as `Edges.http_transport`,
and every provider the federation builds sends through it. A request to a host
nobody scripted raises rather than returning a default: a scenario that
reaches somewhere new has changed, and should say so.

The fixtures here are shaped like the real APIs but hold nothing: a wallet
with no positions on any chain, and no prices. They exist so the real
providers run end to end and produce the citation they produce in production,
which is what decides whether a turn is remembered.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx

Handler = Callable[[httpx.Request], httpx.Response]

INFURA_HOSTS = ("mainnet.infura.io", "base-mainnet.infura.io", "arbitrum-mainnet.infura.io")
BLOCKSCOUT_HOSTS = ("eth.blockscout.com", "base.blockscout.com", "arbitrum.blockscout.com")
PRICE_HOSTS = ("api.coingecko.com",)

EMPTY_WORD = "0x" + "0" * 64
"""An ABI-encoded zero: no balance, no positions, no tokens."""


class UnexpectedEgress(AssertionError):
    """The graph reached a host no fixture answers for."""


def _rpc_result(call: Mapping[str, Any]) -> object:
    method = call.get("method")
    if method == "eth_blockNumber":
        return hex(20_000_000)
    if method == "eth_getLogs":
        return []
    return EMPTY_WORD


def json_rpc(request: httpx.Request) -> httpx.Response:
    """A node that knows of nothing: every read is zero, every log search empty."""
    body = json.loads(request.content)
    calls = body if isinstance(body, list) else [body]
    replies = [{"jsonrpc": "2.0", "id": c.get("id"), "result": _rpc_result(c)} for c in calls]
    return httpx.Response(200, json=replies if isinstance(body, list) else replies[0])


def explorer(request: httpx.Request) -> httpx.Response:
    """A Blockscout that has indexed no position NFTs for anybody."""
    return httpx.Response(200, json={"items": [], "next_page_params": None})


def no_prices(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


def default_fixtures() -> dict[str, Handler]:
    return {
        **dict.fromkeys(INFURA_HOSTS, json_rpc),
        **dict.fromkeys(BLOCKSCOUT_HOSTS, explorer),
        **dict.fromkeys(PRICE_HOSTS, no_prices),
    }


class FakeWeb:
    """Every outbound request the graph makes, answered by host and recorded."""

    def __init__(self, fixtures: Mapping[str, Handler]) -> None:
        self._fixtures = dict(fixtures)
        self.calls: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self._fixtures.get(request.url.host)
        if handler is None:
            raise UnexpectedEgress(f"no fixture for {request.method} {request.url.host}")
        return handler(request)

    def hosts(self, since: int = 0) -> frozenset[str]:
        return frozenset(r.url.host for r in self.calls[since:])

    def rpc_calls(self, since: int = 0) -> list[dict[str, Any]]:
        """Every JSON-RPC call sent to a node, batches flattened, in order."""
        out: list[dict[str, Any]] = []
        for request in self.calls[since:]:
            if request.url.host not in INFURA_HOSTS:
                continue
            body = json.loads(request.content)
            out.extend(body if isinstance(body, list) else [body])
        return out
