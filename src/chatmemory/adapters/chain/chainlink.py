"""BTC and ETH in US dollars from Chainlink's price feeds on Ethereum mainnet.

The fallback for when CoinGecko will not answer -- its keyless price endpoint
began refusing every caller with a CloudFront 403 -- and the source when no
CoinGecko key is configured. Read through the same Infura node the wallet
tools use, so it reaches no host this deployment does not already reach.

What leaves is constant: one `eth_call` to Multicall3 folding `decimals()` and
`latestRoundData()` for both feeds, whichever asset was asked about. Like the
CoinGecko request it replaces, it carries nothing anybody wrote and cannot
reveal which asset, level or person is being watched.

The feed proxies below were checked against the chain when they were added:
`description()` answered "BTC / USD" and "ETH / USD", and `decimals()` 8. The
live integration test asserts the same, so a moved feed fails loudly.

A feed is only updated on a deviation or its heartbeat (one hour for both), so
`updatedAt` is the quote time, and a reading older than the heartbeat plus a
small margin is marked stale rather than presented as current.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import httpx
import structlog

from chatmemory.adapters.chain import abi
from chatmemory.adapters.chain.node import Call, Node
from chatmemory.adapters.chain.tokens import ETHEREUM
from chatmemory.app.clock import Clock, utc_now

log = structlog.get_logger()

CHAINLINK_LABEL = "Chainlink"
DEFAULT_TIMEOUT = 8.0

HEARTBEAT = timedelta(hours=1)
"""How often the BTC/USD and ETH/USD mainnet feeds update with no deviation."""

STALE_MARGIN = timedelta(minutes=5)
"""Slack past the heartbeat before a reading is called stale: an update lands
a block or two after the hour, and that is not a feed that stopped."""

MAX_DECIMALS = 36
"""Any more and `decimals()` is not a price feed's answer."""


@dataclass(frozen=True, slots=True)
class Feed:
    asset: str
    address: str
    description: str
    page: str


FEEDS: Mapping[str, Feed] = MappingProxyType(
    {
        "BTC": Feed(
            "BTC",
            "0xf4030086522a5beea4988f8ca5b36dbc97bee88c",
            "BTC / USD",
            "https://data.chain.link/feeds/ethereum/mainnet/btc-usd",
        ),
        "ETH": Feed(
            "ETH",
            "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            "ETH / USD",
            "https://data.chain.link/feeds/ethereum/mainnet/eth-usd",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class FeedReading:
    """One feed's latest answer, dated by the feed itself."""

    asset: str
    price: Decimal
    updated_at: datetime
    stale: bool
    url: str


class ChainlinkUnavailable(Exception):
    """No feed produced a usable price. The message is safe to log."""


class ChainlinkFeeds:
    """Reads every feed in `FEEDS` in one request. Read-only, like `Node`."""

    def __init__(
        self,
        infura_key: str,
        *,
        endpoint: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._key = infura_key
        self._endpoint = endpoint or f"https://{ETHEREUM.infura_host}.infura.io/v3/{infura_key}"
        self._timeout = timeout_seconds
        self._transport = transport
        self._clock = clock

    @property
    def host(self) -> str:
        """Where the feeds are read, without the key: for egress declarations."""
        return f"https://{httpx.URL(self._endpoint).host}"

    def redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    async def read(self, client: httpx.AsyncClient | None = None) -> dict[str, FeedReading]:
        """Every feed that answered with a positive price, by ticker.

        Raises `ChainlinkUnavailable` when none did or the node failed; never
        returns a reading it could not date.
        """
        try:
            if client is not None:
                readings = await self._read_with(client)
            else:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport
                ) as own:
                    readings = await self._read_with(own)
        except ChainlinkUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any node failure is "unavailable"
            raise ChainlinkUnavailable(self.redact(f"chainlink read failed: {exc}")[:300]) from exc
        if not readings:
            raise ChainlinkUnavailable("chainlink returned no usable price")
        return readings

    async def _read_with(self, client: httpx.AsyncClient) -> dict[str, FeedReading]:
        node = Node(self._endpoint, client, secret=self._key)
        feeds = list(FEEDS.values())
        calls = [
            Call(feed.address, selector)
            for feed in feeds
            for selector in (abi.DECIMALS, abi.FEED_LATEST_ROUND)
        ]
        results = await node.multicall(calls)
        now = self._clock()
        readings: dict[str, FeedReading] = {}
        for i, feed in enumerate(feeds):
            reading = self._reading(feed, results[2 * i], results[2 * i + 1], now)
            if reading is not None:
                readings[feed.asset] = reading
        return readings

    def _reading(
        self, feed: Feed, decimals: bytes | None, round_data: bytes | None, now: datetime
    ) -> FeedReading | None:
        parsed = parse_round(decimals, round_data)
        if parsed is None:
            log.warning("chainlink.unusable_answer", feed=feed.description)
            return None
        price, updated_at = parsed
        return FeedReading(
            asset=feed.asset,
            price=price,
            updated_at=updated_at,
            stale=is_stale(updated_at, now),
            url=feed.page,
        )


def parse_round(
    decimals: bytes | None, round_data: bytes | None
) -> tuple[Decimal, datetime] | None:
    """(price, updatedAt) from `decimals()` and `latestRoundData()`, or None.

    `latestRoundData` is (roundId, answer int256, startedAt, updatedAt,
    answeredInRound). A non-positive answer or a zero `updatedAt` is a round
    that never completed, not a price.
    """
    scale = abi.words(decimals)[:1] if decimals else []
    words = abi.words(round_data) if round_data else []
    if not scale or len(words) < 5 or scale[0] > MAX_DECIMALS:
        return None
    answer = abi.signed(words[1], 256)
    updated = words[3]
    if answer <= 0 or updated <= 0:
        return None
    price = Decimal(answer).scaleb(-scale[0])
    return price, datetime.fromtimestamp(updated, UTC)


def is_stale(updated_at: datetime, now: datetime) -> bool:
    """Older than the feed's heartbeat plus the margin."""
    return now - updated_at > HEARTBEAT + STALE_MARGIN
