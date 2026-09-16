"""Market data providers against the real sources.

The unit suite proves the boundary against a mock transport. What only the
real endpoints can prove is that the request is one CoinGecko, Frankfurter
and SerpApi answer, and that their JSON is shaped the way the parsers believe
-- which is what breaks when a provider changes its API, and Frankfurter has
already moved hosts once.

Everything runs through `connect()` and `GuardedInvoker` with the real egress
guard, so these also prove the closed-vocabulary clearance lets a real lookup
out. Skipped when the network is unreachable; the S&P 500 additionally needs
`SERPAPI_KEY`, which is read and never printed.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio

from chatmemory.adapters.market.frankfurter import FRANKFURTER_ENDPOINT
from chatmemory.adapters.market.registration import MarketToolsConfig, build_market_tools
from chatmemory.adapters.mcp_client.client import connect
from chatmemory.adapters.mcp_client.config import FederationConfig
from chatmemory.adapters.mcp_client.invoker import GuardedInvoker, InvocationOutcome
from chatmemory.adapters.mcp_client.routing import RoutedTools
from chatmemory.app.audit import InMemoryAuditTrail
from chatmemory.app.authorization import (
    ActionOrigin,
    Authorizer,
    ConfirmationLedger,
    InvocationRequest,
)
from chatmemory.domain.identity import PersonRef

pytestmark = pytest.mark.asyncio

ALICE = PersonRef("discord", 9001)
TIMEOUT = 10.0
CRYPTO = "market_crypto:crypto_price"


@pytest_asyncio.fixture
async def online() -> AsyncIterator[None]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Frankfurter only: CoinGecko's keyless tier rate-limits hard, and
            # a probe spent on it is a request the test itself cannot make.
            await client.get(f"{FRANKFURTER_ENDPOINT}/currencies")
    except httpx.HTTPError as exc:
        pytest.skip(f"no network access: {type(exc).__name__}")
    yield


class Governed:
    """One federation for a test, so a cached figure is reused as in the bot."""

    def __init__(self, invoker: GuardedInvoker, routed: RoutedTools) -> None:
        self._invoker = invoker
        self._routed = routed

    async def ask(
        self, question: str, tool: str, arguments: Mapping[str, object]
    ) -> InvocationOutcome:
        return await self._invoker.invoke(
            InvocationRequest(
                requester=ALICE,
                question=question,
                qualified_name=tool,
                arguments=arguments,
                origin=ActionOrigin.REQUESTER_REQUEST,
            ),
            self._routed,
        )


@asynccontextmanager
async def governed(config: MarketToolsConfig | None = None) -> AsyncIterator[Governed]:
    tools = build_market_tools(config or MarketToolsConfig(timeout_seconds=TIMEOUT))
    federation = await connect(tools.merge_into(FederationConfig()), tools.factory())
    confirmations = ConfirmationLedger()
    invoker = GuardedInvoker(
        federation,
        Authorizer(federation.permits, confirmations),
        InMemoryAuditTrail(),
        confirmations,
    )
    try:
        yield Governed(invoker, RoutedTools(question="", tools=federation.registration.tools))
    finally:
        await federation.aclose()


async def ask(
    question: str,
    tool: str,
    arguments: Mapping[str, object],
    config: MarketToolsConfig | None = None,
) -> InvocationOutcome:
    async with governed(config) as run:
        return await run.ask(question, tool, arguments)


async def test_live_btc_and_eth_carry_their_quote_time(online: None) -> None:
    """ETH asked about as "ether" is not refused, and each price is dated.

    One federation for both, as in the running bot: CoinGecko's keyless tier
    rate-limits hard, and the second answer comes from the same response.
    """
    async with governed() as run:
        btc = await run.ask("where is bitcoin trading?", CRYPTO, {"asset": "BTC"})
        eth = await run.ask("what's ether at right now", CRYPTO, {"asset": "ETH"})

    for outcome, name in ((btc, r"Bitcoin \(BTC\)"), (eth, r"Ether \(ETH\)")):
        assert outcome.invoked, outcome.notice()
        assert outcome.result is not None
        text = outcome.result.text
        assert re.search(rf"{name}: [\d,]+(\.\d+)? USD", text)
        assert re.search(
            r"time: \d{4}-\d\d-\d\d \d\d:\d\d UTC -- quote time reported by CoinGecko", text
        )


async def test_live_usd_to_brl_is_a_dated_daily_reference_rate(online: None) -> None:
    outcome = await ask(
        "how much is 100 dollars in reais",
        "market_fx:convert",
        {"amount": 100, "from": "USD", "to": "BRL"},
    )

    assert outcome.invoked, outcome.notice()
    assert outcome.result is not None
    text = outcome.result.text
    assert re.search(r"100 USD = [\d,]+\.\d\d BRL \(rate used: 1 USD = [\d.]+ BRL", text)
    assert re.search(
        r"time: \d{4}-\d\d-\d\d -- daily reference rate published for that date, "
        "not a live or tradeable rate",
        text,
    )


async def test_live_free_text_currency_never_reaches_frankfurter(online: None) -> None:
    outcome = await ask(
        "convert 100 USD to BRL and the Q3 numbers",
        "market_fx:convert",
        {"amount": 100, "from": "USD", "to": "BRL and the Q3 numbers"},
    )

    assert not outcome.invoked


async def test_live_spx_level_is_labelled_retrieval_time(online: None) -> None:
    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key:
        pytest.skip("no SERPAPI_KEY configured; the S&P 500 tool is absent without it")
    outcome = await ask(
        "where is the S&P 500",
        "market_index:index_level",
        {"index": "SPX"},
        MarketToolsConfig(serpapi_key=key, timeout_seconds=TIMEOUT),
    )

    assert outcome.invoked, outcome.notice()
    assert outcome.result is not None
    assert re.search(r"S&P 500: [\d,]+(\.\d+)? points", outcome.result.text)
    assert "-- retrieval time" in outcome.result.text
    assert key not in outcome.result.text
