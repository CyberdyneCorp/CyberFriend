"""A node that holds a few positions, for the reads position alerts make.

The default `json_rpc` fixture answers zero to everything, which is the right
wallet for a positions *lookup* scenario and useless for an alert: an alert
needs a position that is in range, then out of it. This answers the exact
calls the watcher makes -- `ownerOf`, `positions`, `slot0`, the v4 pool and
position info, `getPositionLiquidity`, `getSlot0`, and Aave's pool and
`getUserAccountData` -- from state a scenario can change between sweeps.

Calls it does not model revert, as a contract would: a scenario that reaches
one has changed what it reads, and the watcher reports it as a failed read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.aave import NO_DEBT_HEALTH
from chatmemory.adapters.chain.deployments import DEPLOYMENTS, Deployment
from chatmemory.adapters.chain.node import MULTICALL3

WORD = abi.WORD


@dataclass
class V3Position:
    owner: str
    pool: str
    tick_lower: int
    tick_upper: int
    liquidity: int
    token0: str = "0x4200000000000000000000000000000000000006"
    token1: str = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    fee: int = 500


@dataclass
class V4Position:
    owner: str
    key: tuple[int, int, int, int, int]
    tick_lower: int
    tick_upper: int
    liquidity: int

    @property
    def pool_id(self) -> str:
        return "0x" + abi.keccak256(b"".join(v.to_bytes(32, "big") for v in self.key)).hex()


@dataclass
class AaveAccount:
    collateral_usd: Decimal
    debt_usd: Decimal
    health_factor: Decimal | None


AAVE_POOL = "0xa238dd80c259a72e81d7e4664a9801593f98d1c5"


@dataclass
class FakeChain:
    """One chain's contracts, as far as the watcher reads them."""

    deployment: Deployment = DEPLOYMENTS[1]
    v3: dict[int, V3Position] = field(default_factory=dict)
    v4: dict[int, V4Position] = field(default_factory=dict)
    ticks: dict[str, int] = field(default_factory=dict)
    """Current tick by v3 pool address or v4 pool id."""
    aave: dict[str, AaveAccount] = field(default_factory=dict)
    failing: bool = False
    """Answer every request with HTTP 500."""
    requests: int = 0

    # --- the wire -------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.failing:
            return httpx.Response(500, text="upstream error")
        body = json.loads(request.content)
        calls = body if isinstance(body, list) else [body]
        replies = [{"jsonrpc": "2.0", "id": c.get("id"), "result": self._rpc(c)} for c in calls]
        return httpx.Response(200, json=replies if isinstance(body, list) else replies[0])

    def _rpc(self, call: dict[str, Any]) -> str:
        if call.get("method") != "eth_call":
            return "0x" + "0" * 64
        params = call["params"][0]
        target, data = params["to"].lower(), params["data"]
        if target == MULTICALL3:
            results = [self._answer(t, d) for t, d in decode_aggregate3_call(data)]
            return "0x" + encode_aggregate3_result(results).hex()
        answer = self._answer(target, data)
        return "0x" + (answer or b"").hex()

    # --- the contracts ----------------------------------------------------

    def _answer(self, target: str, data: str) -> bytes | None:
        selector, args = data[:10], _words(data[10:])
        d = self.deployment
        if target == d.aave_addresses_provider and selector == abi.AAVE_GET_POOL:
            return _encode(int(AAVE_POOL, 16))
        if target == AAVE_POOL and selector == abi.AAVE_ACCOUNT_DATA:
            return self._account(abi.as_address(args[0]))
        if target == d.v3_position_manager:
            return self._v3(selector, args[0])
        if target == d.v4_position_manager:
            return self._v4(selector, args[0])
        if target == d.v4_state_view and selector == abi.V4_SLOT0:
            return self._slot0("0x" + data[10:74])
        if selector == abi.V3_SLOT0:
            return self._slot0(target)
        return None

    def _slot0(self, pool: str) -> bytes | None:
        tick = self.ticks.get(pool.lower())
        return None if tick is None else _encode(1 << 96, tick, 0, 0, 0, 0, 1)

    def _v3(self, selector: str, token_id: int) -> bytes | None:
        p = self.v3.get(token_id)
        if p is None:
            return None
        if selector == abi.OWNER_OF:
            return _encode(int(p.owner, 16))
        if selector == abi.V3_POSITIONS:
            return _encode(
                0, 0, int(p.token0, 16), int(p.token1, 16), p.fee,
                p.tick_lower, p.tick_upper, p.liquidity, 0, 0, 0, 0,
            )
        return None

    def _v4(self, selector: str, token_id: int) -> bytes | None:
        p = self.v4.get(token_id)
        if p is None:
            return None
        if selector == abi.OWNER_OF:
            return _encode(int(p.owner, 16))
        if selector == abi.V4_POOL_AND_POSITION:
            info = ((p.tick_lower & 0xFFFFFF) << 8) | ((p.tick_upper & 0xFFFFFF) << 32)
            return _encode(*p.key, info)
        if selector == abi.V4_POSITION_LIQUIDITY:
            return _encode(p.liquidity)
        return None

    def _account(self, user: str) -> bytes | None:
        a = self.aave.get(user.lower())
        if a is None:
            return _encode(0, 0, 0, 0, 0, NO_DEBT_HEALTH)
        health = NO_DEBT_HEALTH if a.health_factor is None else int(a.health_factor * 10**18)
        return _encode(
            int(a.collateral_usd * 10**8), int(a.debt_usd * 10**8), 0, 8250, 7800, health
        )


# --- ABI helpers the watcher has no need of ----------------------------------


def _words(hex_data: str) -> list[int]:
    return abi.words(bytes.fromhex(hex_data)) if hex_data else [0]


def _encode(*values: int) -> bytes:
    return bytes.fromhex("".join(abi.uint(v) for v in values))


def decode_aggregate3_call(data: str) -> list[tuple[str, str]]:
    """(target, calldata) pairs from an `aggregate3` call, as `abi.aggregate3` wrote them."""
    raw = bytes.fromhex(data.removeprefix("0x")[8:])

    def at(offset: int) -> int:
        return int.from_bytes(raw[offset : offset + WORD], "big")

    base = at(0)
    count = at(base)
    out: list[tuple[str, str]] = []
    for i in range(count):
        element = base + WORD + at(base + WORD + WORD * i)
        target = abi.as_address(at(element))
        start = element + at(element + 2 * WORD)
        length = at(start)
        out.append((target, "0x" + raw[start + WORD : start + WORD + length].hex()))
    return out


def encode_aggregate3_result(results: list[bytes | None]) -> bytes:
    """`(bool success, bytes returnData)[]`, the shape `abi.decode_aggregate3` reads."""
    elements: list[bytes] = []
    for result in results:
        payload = result or b""
        padded = payload + b"\0" * (-len(payload) % WORD)
        head = _encode(1 if result is not None else 0, 2 * WORD, len(payload))
        elements.append(head + padded)
    offsets: list[int] = []
    offset = WORD * len(results)
    for element in elements:
        offsets.append(offset)
        offset += len(element)
    return _encode(WORD, len(results), *offsets) + b"".join(elements)
