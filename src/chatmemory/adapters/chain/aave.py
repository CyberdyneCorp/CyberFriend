"""Aave v3 supplies, borrows and account health for one address on one chain.

Also the price source for liquidity positions: the Aave oracle quotes every
reserve in USD on the chain being read, so valuing a WETH/USDC range costs no
request to anyone but the node already being asked.
"""

from __future__ import annotations

from decimal import Decimal

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.deployments import NATIVE, Deployment
from chatmemory.adapters.chain.liquidity_math import apy_from_ray, whole
from chatmemory.adapters.chain.node import Call, Node, NodeError
from chatmemory.adapters.chain.positions import ChainLending, LendingAsset, TokenDirectory

BASE_CURRENCY_DECIMALS = 8
"""Aave v3 markets on these chains quote the base currency as USD with 8
decimals, for account totals and the oracle alike."""

HEALTH_DECIMALS = 18
NO_DEBT_HEALTH = (1 << 256) - 1


class AaveReader:
    def __init__(self, node: Node, deployment: Deployment, tokens: TokenDirectory) -> None:
        self._node = node
        self._deployment = deployment
        self._tokens = tokens
        self._contracts: tuple[str, str, str] | None = None

    async def contracts(self) -> tuple[str, str, str]:
        """Pool, data provider and oracle, from the addresses provider."""
        if self._contracts is None:
            provider = self._deployment.aave_addresses_provider
            pool, data, oracle = await self._node.multicall([
                Call(provider, abi.AAVE_GET_POOL),
                Call(provider, abi.AAVE_GET_DATA_PROVIDER),
                Call(provider, abi.AAVE_GET_ORACLE),
            ])
            if not (pool and data and oracle):
                raise NodeError("Aave addresses provider did not answer")
            self._contracts = (
                abi.as_address(abi.words(pool)[0]),
                abi.as_address(abi.words(data)[0]),
                abi.as_address(abi.words(oracle)[0]),
            )
        return self._contracts

    async def usd_prices(self, assets: set[str]) -> dict[str, Decimal]:
        """USD per whole token for each asset the oracle prices.

        Native ether is asked for as WETH. An asset the oracle does not know
        is absent from the result, never zero.
        """
        _, _, oracle = await self.contracts()
        wanted = sorted({a.lower() for a in assets})
        asked = [self._deployment.weth if a == NATIVE else a for a in wanted]
        results = await self._node.multicall(
            [Call(oracle, abi.call(abi.AAVE_ASSET_PRICE, abi.address(a))) for a in asked]
        )
        prices: dict[str, Decimal] = {}
        for asset, raw in zip(wanted, results, strict=True):
            value = abi.words(raw)[0] if raw else 0
            if value:
                prices[asset] = whole(value, BASE_CURRENCY_DECIMALS)
        return prices

    async def lending(self, user: str) -> ChainLending:
        pool, data, _ = await self.contracts()
        account_raw = await self._node.eth_call(
            pool, abi.call(abi.AAVE_ACCOUNT_DATA, abi.address(user))
        )
        collateral, debt, available, _, _, health = abi.words(account_raw)[:6]
        reserves = abi.decode_address_array(
            await self._node.eth_call(pool, abi.AAVE_RESERVES_LIST)
        )
        held = await self._held(data, reserves, user)
        assets = await self._describe(data, held)
        return ChainLending(
            chain=self._deployment.chain,
            collateral_usd=whole(collateral, BASE_CURRENCY_DECIMALS),
            debt_usd=whole(debt, BASE_CURRENCY_DECIMALS),
            available_usd=whole(available, BASE_CURRENCY_DECIMALS),
            health_factor=None if health == NO_DEBT_HEALTH or not debt
            else whole(health, HEALTH_DECIMALS),
            assets=assets,
        )

    async def _held(
        self, data: str, reserves: list[str], user: str
    ) -> list[tuple[str, int, int, bool]]:
        """(asset, supplied raw, borrowed raw, used as collateral) where non-zero."""
        results = await self._node.multicall([
            Call(data, abi.call(abi.AAVE_USER_RESERVE, abi.address(r), abi.address(user)))
            for r in reserves
        ])
        held: list[tuple[str, int, int, bool]] = []
        for asset, raw in zip(reserves, results, strict=True):
            if not raw:
                continue
            w = abi.words(raw)
            supplied, borrowed = w[0], w[1] + w[2]  # aToken; stable + variable debt
            if supplied or borrowed:
                held.append((asset, supplied, borrowed, bool(w[8])))
        return held

    async def _describe(
        self, data: str, held: list[tuple[str, int, int, bool]]
    ) -> tuple[LendingAsset, ...]:
        if not held:
            return ()
        addresses = {h[0] for h in held}
        tokens = await self._tokens.load(addresses)
        prices = await self.usd_prices(addresses)
        rates = await self._node.multicall(
            [Call(data, abi.call(abi.AAVE_RESERVE_DATA, abi.address(h[0]))) for h in held]
        )
        assets: list[LendingAsset] = []
        for (asset, supplied, borrowed, collateral), raw in zip(held, rates, strict=True):
            token = tokens[asset.lower()]
            w = abi.words(raw) if raw else []
            assets.append(
                LendingAsset(
                    token=token,
                    supplied=whole(supplied, token.decimals),
                    borrowed=whole(borrowed, token.decimals),
                    supply_apy=apy_from_ray(w[5]) if len(w) > 6 else Decimal(0),
                    borrow_apy=apy_from_ray(w[6]) if len(w) > 6 else Decimal(0),
                    usd_price=prices.get(asset.lower()),
                    collateral=collateral,
                )
            )
        return tuple(assets)
