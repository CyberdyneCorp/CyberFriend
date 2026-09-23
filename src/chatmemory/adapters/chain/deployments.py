"""Where Uniswap and Aave live on each chain this package reads.

Only the entry points are written down. Aave's pool, data provider and oracle
are resolved from its addresses provider at read time, so an Aave upgrade that
moves them is followed rather than silently read from the old contracts.

Every address here was checked against the chain when it was added: the
position managers answer `balanceOf`, the addresses providers answer `getPool`.
"""

from __future__ import annotations

from dataclasses import dataclass

from chatmemory.adapters.chain.tokens import ARBITRUM, BASE, ETHEREUM, Chain

NATIVE = "0x0000000000000000000000000000000000000000"
"""How Uniswap v4 names native ether as a pool currency."""


@dataclass(frozen=True, slots=True)
class Deployment:
    chain: Chain
    v3_position_manager: str
    v3_factory: str
    v4_position_manager: str
    v4_state_view: str
    aave_addresses_provider: str
    #: Wrapped ether: how native ETH is priced by the Aave oracle.
    weth: str
    #: Blockscout instance used only to *find* v4 position IDs.
    blockscout: str


DEPLOYMENTS: tuple[Deployment, ...] = (
    Deployment(
        chain=ETHEREUM,
        v3_position_manager="0xc36442b4a4522e871399cd717abdd847ab11fe88",
        v3_factory="0x1f98431c8ad98523631ae4a59f267346ea31f984",
        v4_position_manager="0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e",
        v4_state_view="0x7ffe42c4a5deea5b0fec41c94c136cf115597227",
        aave_addresses_provider="0x2f39d218133afab8f2b819b1066c7e434ad94e9e",
        weth="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        blockscout="https://eth.blockscout.com",
    ),
    Deployment(
        chain=BASE,
        v3_position_manager="0x03a520b32c04bf3beef7beb72e919cf822ed34f1",
        v3_factory="0x33128a8fc17869897dce68ed026d694621f6fdfd",
        v4_position_manager="0x7c5f5a4bbd8fd63184577525326123b519429bdc",
        v4_state_view="0xa3c0c9b65bad0b08107aa264b0f3db444b867a71",
        aave_addresses_provider="0xe20fcbdbffc4dd138ce8b2e6fbb6cb49777ad64d",
        weth="0x4200000000000000000000000000000000000006",
        blockscout="https://base.blockscout.com",
    ),
    Deployment(
        chain=ARBITRUM,
        v3_position_manager="0xc36442b4a4522e871399cd717abdd847ab11fe88",
        v3_factory="0x1f98431c8ad98523631ae4a59f267346ea31f984",
        v4_position_manager="0xd88f38f930b7952f2db2432cb002e7abbf3dd869",
        v4_state_view="0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        aave_addresses_provider="0xa97684ead0e402dc232d5a977953df7ecbab3cdb",
        weth="0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        blockscout="https://arbitrum.blockscout.com",
    ),
)
