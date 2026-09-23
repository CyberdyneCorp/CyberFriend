"""The few pieces of the Solidity ABI this package needs, and no more.

Written out rather than taken from web3.py: the whole surface is static words,
one dynamic array and a string, and a dependency that can also build and sign
transactions is a capability this package is deliberately without.

Selectors are constants, not computed at import. `tests/unit/test_chain_abi.py`
recomputes each one from its signature, so a typo cannot survive.
"""

from __future__ import annotations

from Crypto.Hash import keccak

WORD = 32
UINT256 = 1 << 256
UINT128_MAX = (1 << 128) - 1

# ERC-20 / ERC-721
BALANCE_OF = "0x70a08231"  # balanceOf(address)
DECIMALS = "0x313ce567"  # decimals()
SYMBOL = "0x95d89b41"  # symbol()
OWNER_OF = "0x6352211e"  # ownerOf(uint256)
TOKEN_OF_OWNER_BY_INDEX = "0x2f745c59"  # tokenOfOwnerByIndex(address,uint256)

# Multicall3
AGGREGATE3 = "0x82ad56cb"  # aggregate3((address,bool,bytes)[])

# Uniswap v3
V3_POSITIONS = "0x99fbab88"  # positions(uint256)
V3_GET_POOL = "0x1698ee82"  # getPool(address,address,uint24)
V3_SLOT0 = "0x3850c7bd"  # slot0()
V3_COLLECT = "0xfc6f7865"  # collect((uint256,address,uint128,uint128))

# Uniswap v4
V4_POOL_AND_POSITION = "0x7ba03aad"  # getPoolAndPositionInfo(uint256)
V4_POSITION_LIQUIDITY = "0x1efeed33"  # getPositionLiquidity(uint256)
V4_SLOT0 = "0xc815641c"  # getSlot0(bytes32)
V4_POSITION_INFO = "0xdacf1d2f"  # getPositionInfo(bytes32,address,int24,int24,bytes32)
V4_FEE_GROWTH_INSIDE = "0x53e9c1fb"  # getFeeGrowthInside(bytes32,int24,int24)

# Aave v3
AAVE_GET_POOL = "0x026b1d5f"  # getPool()
AAVE_GET_DATA_PROVIDER = "0xe860accb"  # getPoolDataProvider()
AAVE_GET_ORACLE = "0xfca513a8"  # getPriceOracle()
AAVE_ACCOUNT_DATA = "0xbf92857c"  # getUserAccountData(address)
AAVE_RESERVES_LIST = "0xd1946dbc"  # getReservesList()
AAVE_USER_RESERVE = "0x28dd2d01"  # getUserReserveData(address,address)
AAVE_RESERVE_DATA = "0x35ea6a75"  # getReserveData(address)
AAVE_ASSET_PRICE = "0xb3596f07"  # getAssetPrice(address)


def selector(signature: str) -> str:
    """The 4-byte selector of a function signature. For tests and one-offs."""
    return "0x" + keccak256(signature.encode())[:4].hex()


def keccak256(data: bytes) -> bytes:
    digest = keccak.new(digest_bits=256)
    digest.update(data)
    return digest.digest()


# --- encoding ------------------------------------------------------------


def uint(value: int) -> str:
    """One word, as hex without a prefix. Negative values wrap, as int24 does."""
    return (value % UINT256).to_bytes(WORD, "big").hex()


def address(value: str) -> str:
    return value.lower().removeprefix("0x").rjust(64, "0")


def call(selector_hex: str, *words: str) -> str:
    return selector_hex + "".join(words)


def aggregate3(calls: list[tuple[str, str]]) -> str:
    """`aggregate3` over (target, calldata) pairs, every call allowed to fail.

    Allowed to fail because one reverting call -- a token with no `symbol()`,
    an asset the oracle does not price -- must cost that one figure, not the
    whole chunk.
    """
    elements: list[str] = []
    for target, data in calls:
        raw = bytes.fromhex(data.removeprefix("0x"))
        padded = raw + b"\0" * (-len(raw) % WORD)
        elements.append(address(target) + uint(1) + uint(3 * WORD) + uint(len(raw)) + padded.hex())
    offsets: list[str] = []
    offset = WORD * len(calls)
    for element in elements:
        offsets.append(uint(offset))
        offset += len(element) // 2
    return AGGREGATE3 + uint(WORD) + uint(len(calls)) + "".join(offsets) + "".join(elements)


# --- decoding ------------------------------------------------------------


def to_bytes(hex_value: str) -> bytes:
    return bytes.fromhex(hex_value.removeprefix("0x"))


def words(data: bytes) -> list[int]:
    return [int.from_bytes(data[i : i + WORD], "big") for i in range(0, len(data) - WORD + 1, WORD)]


def signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def as_address(word: int) -> str:
    return "0x" + (word & ((1 << 160) - 1)).to_bytes(20, "big").hex()


def decode_aggregate3(data: bytes) -> list[bytes | None]:
    """Each call's return data, or None where that call reverted."""

    def at(offset: int) -> int:
        return int.from_bytes(data[offset : offset + WORD], "big")

    base = at(0)
    count = at(base)
    out: list[bytes | None] = []
    for i in range(count):
        element = base + WORD + at(base + WORD + WORD * i)
        ok = at(element)
        start = element + at(element + WORD)
        length = at(start)
        out.append(data[start + WORD : start + WORD + length] if ok else None)
    return out


def decode_address_array(data: bytes) -> list[str]:
    """A returned `address[]`."""
    values = words(data)
    if len(values) < 2:
        return []
    count = values[values[0] // WORD]
    first = values[0] // WORD + 1
    return [as_address(w) for w in values[first : first + count]]


def decode_string(data: bytes) -> str:
    """A returned `string`, or a `bytes32` for the tokens that predate one."""
    if len(data) == WORD:
        return data.rstrip(b"\0").decode("utf-8", "replace")
    if len(data) < 2 * WORD:
        return ""
    offset = int.from_bytes(data[:WORD], "big")
    length = int.from_bytes(data[offset : offset + WORD], "big")
    return data[offset + WORD : offset + WORD + length].decode("utf-8", "replace")
