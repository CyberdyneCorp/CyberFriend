"""The Chainlink BTC/USD and ETH/USD feeds on Ethereum mainnet, read for real.

What only the chain can prove: that the proxy addresses in
`adapters.chain.chainlink.FEEDS` are the feeds they claim to be (their own
`description()` says "BTC / USD" and "ETH / USD", with 8 decimals), that the
`latestRoundData()` decoding matches what they return, and that the market
tool answers from them when CoinGecko is not asked -- the path production
takes while CoinGecko's keyless endpoint refuses every caller.

Needs `INFURA_KEY` (read, never printed); skipped without it or without a
network. Prices move, so the assertions are about shape and freshness.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

import httpx
import pytest

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.chainlink import FEEDS, HEARTBEAT, STALE_MARGIN, ChainlinkFeeds
from chatmemory.adapters.chain.node import Node
from chatmemory.adapters.market.registration import MarketToolsConfig
from tests.integration.test_market_live import CRYPTO, TIMEOUT, governed

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("INFURA_KEY", "").strip(), reason="needs INFURA_KEY"),
]


def infura_key() -> str:
    return os.environ["INFURA_KEY"].strip()


async def test_live_feed_addresses_are_the_btc_and_eth_usd_feeds() -> None:
    key = infura_key()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            node = Node(f"https://mainnet.infura.io/v3/{key}", client, secret=key)
            for feed in FEEDS.values():
                description = abi.decode_string(
                    await node.eth_call(feed.address, abi.FEED_DESCRIPTION)
                )
                decimals = abi.words(await node.eth_call(feed.address, abi.DECIMALS))[0]
                assert (description, decimals) == (feed.description, 8), feed.asset
    except httpx.TransportError as exc:
        pytest.skip(f"no network access: {type(exc).__name__}")


async def test_live_feeds_read_a_fresh_positive_price_for_btc_and_eth() -> None:
    try:
        readings = await ChainlinkFeeds(infura_key(), timeout_seconds=TIMEOUT).read()
    except httpx.TransportError as exc:
        pytest.skip(f"no network access: {type(exc).__name__}")

    assert set(readings) == {"BTC", "ETH"}
    now = datetime.now(UTC)
    for reading in readings.values():
        assert reading.price > 0
        assert now - reading.updated_at <= HEARTBEAT + STALE_MARGIN, reading
        assert not reading.stale
    assert readings["BTC"].price > readings["ETH"].price


async def test_live_price_tool_answers_from_chainlink_without_a_coingecko_key() -> None:
    config = MarketToolsConfig(infura_key=infura_key(), timeout_seconds=TIMEOUT)
    async with governed(config) as run:
        btc = await run.ask("where is bitcoin trading?", CRYPTO, {"asset": "BTC"})
        eth = await run.ask("what's ether at right now", CRYPTO, {"asset": "ETH"})

    for outcome, name in ((btc, r"Bitcoin \(BTC\)"), (eth, r"Ether \(ETH\)")):
        assert outcome.invoked, outcome.notice()
        assert outcome.result is not None
        text = outcome.result.text
        assert re.search(rf"{name}: [\d,]+\.\d\d USD", text)
        assert re.search(
            r"time: \d{4}-\d\d-\d\d \d\d:\d\d UTC -- quote time reported by Chainlink\n", text
        )
        assert infura_key() not in text
