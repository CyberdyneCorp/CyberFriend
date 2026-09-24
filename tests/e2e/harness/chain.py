"""A node that holds a few positions, for the reads position alerts make.

The default `json_rpc` fixture answers zero to everything, which is the right
wallet for a positions *lookup* scenario and useless for an alert: an alert
needs a position that is in range, then out of it. This answers the exact
calls the watcher makes -- `ownerOf`, `positions`, `slot0`, the v4 pool and
position info, `getPositionLiquidity`, `getSlot0`, and Aave's pool and
`getUserAccountData` -- from state a scenario can change between sweeps.

It also answers what creating a range alert reads once, through the positions
reader: an owner's v3 `balanceOf` and `tokenOfOwnerByIndex`, the factory's
`getPool`, the simulated `collect`, and the tokens' `symbol` and `decimals`.

And what a portfolio reads besides: the native balance, ERC-20 `balanceOf`,
Aave's reserve list, per-reserve user data and rates from the data provider,
and the oracle's prices. Figures are whole units and dollars, converted here,
so a scenario reads like the answer it expects.

Calls it does not model revert, as a contract would: a scenario that reaches
one has changed what it reads, and the watcher reports it as a failed read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
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


@dataclass
class AaveReserve:
    """One person's supply and borrow of one reserve, in whole units."""

    supplied: Decimal = Decimal(0)
    borrowed: Decimal = Decimal(0)
    collateral: bool = True


AAVE_POOL = "0xa238dd80c259a72e81d7e4664a9801593f98d1c5"
AAVE_DATA_PROVIDER = "0x0f43731eb8d45a581f4a36dd74f5f358bc90c73a"
AAVE_ORACLE = "0x2cc0fc26ed4563a5ce5e8bdcfe1a2878676ae156"

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
"""On Base: an Aave reserve, and outside the named token set."""

TOKENS = {
    WETH: ("WETH", 18),
    USDC: ("USDC", 6),
    CBBTC: ("cbBTC", 8),
}

_NOT_MODELLED = object()
"""A call this group of contracts does not answer; `None` is a revert."""


@dataclass
class FakeChain:
    """One chain's contracts, as far as the watcher reads them."""

    deployment: Deployment = DEPLOYMENTS[1]
    v3: dict[int, V3Position] = field(default_factory=dict)
    v4: dict[int, V4Position] = field(default_factory=dict)
    ticks: dict[str, int] = field(default_factory=dict)
    """Current tick by v3 pool address or v4 pool id."""
    aave: dict[str, AaveAccount] = field(default_factory=dict)
    tokens: dict[str, tuple[str, int]] = field(default_factory=lambda: dict(TOKENS))
    """Symbol and decimals by token address."""
    native: dict[str, Decimal] = field(default_factory=dict)
    """ETH held, by owner."""
    balances: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    """ERC-20 held, by (token, owner), in whole units."""
    reserves: list[str] = field(default_factory=lambda: [WETH, USDC, CBBTC])
    """Aave's reserve list: underlyings, as `getReservesList` returns them."""
    prices: dict[str, Decimal] = field(default_factory=dict)
    """The Aave oracle's USD price by asset. Unlisted assets price at zero."""
    lending: dict[tuple[str, str], AaveReserve] = field(default_factory=dict)
    """Aave supplies and borrows by (owner, asset)."""
    failing: bool = False
    """Answer every request with HTTP 500."""
    failing_selectors: set[str] = field(default_factory=set)
    """Direct `eth_call`s with these selectors answer a JSON-RPC error."""
    requests: int = 0

    # --- the wire -------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.failing:
            return httpx.Response(500, text="upstream error")
        body = json.loads(request.content)
        calls = body if isinstance(body, list) else [body]
        replies = [self._reply(c) for c in calls]
        return httpx.Response(200, json=replies if isinstance(body, list) else replies[0])

    def _reply(self, call: dict[str, Any]) -> dict[str, Any]:
        params = call.get("params") or [{}]
        data = params[0].get("data", "") if isinstance(params[0], dict) else ""
        if call.get("method") == "eth_call" and data[:10] in self.failing_selectors:
            error = {"code": -32000, "message": "execution reverted"}
            return {"jsonrpc": "2.0", "id": call.get("id"), "error": error}
        return {"jsonrpc": "2.0", "id": call.get("id"), "result": self._rpc(call)}

    def _rpc(self, call: dict[str, Any]) -> str:
        if call.get("method") == "eth_getBalance":
            owner = str(call["params"][0]).lower()
            return hex(int(self.native.get(owner, Decimal(0)) * 10**18))
        if call.get("method") != "eth_call":
            return "0x" + "0" * 64
        params = call["params"][0]
        target, data = params["to"].lower(), params["data"]
        if target == MULTICALL3:
            results = [self._dispatch(t, d) for t, d in decode_aggregate3_call(data)]
            return "0x" + encode_aggregate3_result(results).hex()
        answer = self._dispatch(target, data)
        return "0x" + (answer or b"").hex()

    # --- the contracts ----------------------------------------------------

    def _answer(self, target: str, data: str) -> bytes | None:
        selector, args = data[:10], _words(data[10:])
        d = self.deployment
        if target == d.aave_addresses_provider and selector == abi.AAVE_GET_POOL:
            return _encode(int(AAVE_POOL, 16))
        if target == AAVE_POOL and selector == abi.AAVE_ACCOUNT_DATA:
            return self._account(abi.as_address(args[0]))
        if selector == abi.BALANCE_OF and target == d.v3_position_manager:
            return _encode(len(self._owned_v3(abi.as_address(args[0]))))
        if selector == abi.BALANCE_OF and target == d.v4_position_manager:
            owner = abi.as_address(args[0])
            return _encode(sum(p.owner.lower() == owner for p in self.v4.values()))
        if target == d.v3_position_manager and selector == abi.TOKEN_OF_OWNER_BY_INDEX:
            owned = self._owned_v3(abi.as_address(args[0]))
            return _encode(owned[args[1]]) if args[1] < len(owned) else None
        if target == d.v3_position_manager and selector == abi.V3_COLLECT:
            return _encode(0, 0)
        if target == d.v3_factory and selector == abi.V3_GET_POOL:
            return self._pool(abi.as_address(args[0]), abi.as_address(args[1]), args[2])
        if target in self.tokens and selector in (abi.SYMBOL, abi.DECIMALS):
            symbol, decimals = self.tokens[target]
            if selector == abi.DECIMALS:
                return _encode(decimals)
            return symbol.encode().ljust(WORD, b"\0")
        if target == d.v3_position_manager:
            return self._v3(selector, args[0])
        if target == d.v4_position_manager:
            return self._v4(selector, args[0])
        if target == d.v4_state_view and selector == abi.V4_SLOT0:
            return self._slot0("0x" + data[10:74])
        if selector == abi.V3_SLOT0:
            return self._slot0(target)
        return None

    def _dispatch(self, target: str, data: str) -> bytes | None:
        answer = self._portfolio_answer(target, data[:10], _words(data[10:]))
        if isinstance(answer, bytes):
            return answer
        return self._answer(target, data)

    def _portfolio_answer(self, target: str, selector: str, args: list[int]) -> object:
        """The Aave reads beyond the account, and token balances."""
        provider = self.deployment.aave_addresses_provider
        answers: dict[tuple[str, str], Callable[[], bytes]] = {
            (provider, abi.AAVE_GET_DATA_PROVIDER): lambda: _encode(int(AAVE_DATA_PROVIDER, 16)),
            (provider, abi.AAVE_GET_ORACLE): lambda: _encode(int(AAVE_ORACLE, 16)),
            (AAVE_POOL, abi.AAVE_RESERVES_LIST): self._reserve_list,
            (AAVE_DATA_PROVIDER, abi.AAVE_USER_RESERVE): lambda: self._user_reserve(args),
            (AAVE_DATA_PROVIDER, abi.AAVE_RESERVE_DATA): lambda: _encode(*[0] * 12),
            (AAVE_ORACLE, abi.AAVE_ASSET_PRICE): lambda: self._price(args),
        }
        answer = answers.get((target, selector))
        if answer is not None:
            return answer()
        if selector == abi.BALANCE_OF and target in self.tokens:
            owner = abi.as_address(args[0])
            held = self.balances.get((target, owner), Decimal(0))
            return _encode(int(held * 10 ** self.tokens[target][1]))
        return _NOT_MODELLED

    def _reserve_list(self) -> bytes:
        return _encode(WORD, len(self.reserves), *(int(r, 16) for r in self.reserves))

    def _user_reserve(self, args: list[int]) -> bytes:
        asset, owner = abi.as_address(args[0]), abi.as_address(args[1])
        entry = self.lending.get((owner, asset), AaveReserve())
        decimals = self.tokens[asset][1]
        supplied, borrowed = (int(v * 10**decimals) for v in (entry.supplied, entry.borrowed))
        return _encode(supplied, 0, borrowed, 0, 0, 0, 0, 0, int(entry.collateral))

    def _price(self, args: list[int]) -> bytes:
        return _encode(int(self.prices.get(abi.as_address(args[0]), Decimal(0)) * 10**8))

    def _owned_v3(self, owner: str) -> list[int]:
        return sorted(t for t, p in self.v3.items() if p.owner.lower() == owner.lower())

    def _pool(self, token0: str, token1: str, fee: int) -> bytes | None:
        for p in self.v3.values():
            if (p.token0.lower(), p.token1.lower(), p.fee) == (token0, token1, fee):
                return _encode(int(p.pool, 16))
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
