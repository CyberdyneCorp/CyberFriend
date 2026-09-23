"""DeFi positions against the real chains, through the real guard.

What only the chains can prove: that the contract addresses are right, that
the ABI decoding matches what they return, and that Blockscout still answers
the v4 lookup. Everything runs through `connect()` and `GuardedInvoker` with
the real egress guard, so this also proves a rooted address is let out and an
unrooted one is not.

Needs `INFURA_KEY` (read, never printed); skipped without it. The wallet is a
real one with an Aave loan and open Uniswap v3 ranges on Base. Its positions
will change, so the assertions are about shape, not figures.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import pytest

from chatmemory.adapters.chain.registration import ChainToolsConfig, build_chain_tools
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

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("INFURA_KEY"), reason="needs INFURA_KEY"),
]

ALICE = PersonRef("discord", 9001)
WALLET = "0xDD8aC30cb0a219AF4963eAB5e30ED065b5E63d68"
OTHER = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"


async def ask(question: str, tool: str, arguments: Mapping[str, object]) -> InvocationOutcome:
    tools = build_chain_tools(ChainToolsConfig(infura_key=os.environ["INFURA_KEY"]))
    federation = await connect(tools.merge_into(FederationConfig()), tools.factory())
    confirmations = ConfirmationLedger()
    invoker = GuardedInvoker(
        federation,
        Authorizer(federation.permits, confirmations),
        InMemoryAuditTrail(),
        confirmations,
    )
    try:
        return await invoker.invoke(
            InvocationRequest(
                requester=ALICE,
                question=question,
                qualified_name=tool,
                arguments=arguments,
                origin=ActionOrigin.REQUESTER_REQUEST,
            ),
            RoutedTools(question="", tools=federation.registration.tools),
        )
    finally:
        await federation.aclose()


async def test_live_liquidity_and_aave_positions() -> None:
    outcome = await ask(
        f"defi positions of {WALLET}", "defi_positions:defi_positions", {"address": WALLET}
    )
    assert outcome.invoked, outcome.notice()
    assert outcome.result is not None
    text = outcome.result.text

    for chain in ("Ethereum", "Base", "Arbitrum"):
        assert f"**{chain}**" in text
    assert "could not be read" not in text, text
    # Liquidity: every field that was asked for, on at least one position.
    assert "Uniswap v3 #" in text
    assert "per WETH" in text and " · now " in text
    assert "in range" in text or "out of range" in text
    assert "Uncollected:" in text and "≈ $" in text
    # Aave: the account, and both sides of it.
    assert "Aave v3" in text and "Health factor" in text
    assert "Supplied:" in text


async def test_live_an_address_the_asker_did_not_write_is_not_looked_up() -> None:
    outcome = await ask(
        f"liquidity positions of {WALLET}",
        "defi_positions:liquidity_positions",
        {"address": OTHER},
    )
    # The clearance is the question's address; the model's argument is not
    # what is read. Whatever ran, it was never about OTHER.
    if outcome.result is not None:
        assert OTHER.lower() not in outcome.result.text.lower()
