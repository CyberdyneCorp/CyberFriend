"""The tokens this deployment can see, and the honest limit that implies.

JSON-RPC cannot enumerate what an address holds. `eth_getBalance` gives the
native balance; everything else requires knowing a contract to call `balanceOf`
on. A provider with an indexed view could discover holdings; a node cannot.

So the set is named here, per chain, and a token is reported because somebody
listed it. That limit is stated in the answer rather than hidden: a token
nobody listed reads as absent, and an answer that quietly omitted it would be
wrong in the direction people care about.

Chosen for what wallets on these two chains actually hold: the dollar
stablecoins, and wrapped ether because a balance held as WETH is invisible to
`eth_getBalance` and is the one omission somebody would notice immediately.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Chain:
    """One chain this deployment can read."""

    key: str
    name: str
    #: The Infura host segment. The endpoint is built from this and the key,
    #: so an endpoint is never written down with a secret in it.
    infura_host: str
    native_symbol: str
    #: What the native asset is called to the price source, or "" when this
    #: chain's native asset has no price we look up.
    price_symbol: str
    explorer: str


ETHEREUM = Chain(
    key="ethereum",
    name="Ethereum",
    infura_host="mainnet",
    native_symbol="ETH",
    price_symbol="ETH",
    explorer="https://etherscan.io/address/",
)

BASE = Chain(
    key="base",
    name="Base",
    infura_host="base-mainnet",
    native_symbol="ETH",
    # Base's native asset is ether, so it is priced as ether.
    price_symbol="ETH",
    explorer="https://basescan.org/address/",
)

CHAINS: tuple[Chain, ...] = (ETHEREUM, BASE)


@dataclass(frozen=True, slots=True)
class Token:
    """An ERC-20 this deployment knows how to ask about."""

    symbol: str
    contract: str
    decimals: int
    #: A stablecoin is worth a dollar closely enough to say so without a
    #: price lookup. Anything else needs one, and says so.
    dollar_pegged: bool = False


TOKENS: dict[str, tuple[Token, ...]] = {
    ETHEREUM.key: (
        Token("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6, dollar_pegged=True),
        Token("USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", 6, dollar_pegged=True),
        Token("DAI", "0x6b175474e89094c44da98b954eedeac495271d0f", 18, dollar_pegged=True),
        Token("WETH", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 18),
    ),
    BASE.key: (
        Token("USDC", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6, dollar_pegged=True),
        Token("DAI", "0x50c5725949a6f0c72e6c4a641f24049a917db0cb", 18, dollar_pegged=True),
        Token("WETH", "0x4200000000000000000000000000000000000006", 18),
    ),
}


def tokens_for(chain: Chain) -> tuple[Token, ...]:
    return TOKENS.get(chain.key, ())
