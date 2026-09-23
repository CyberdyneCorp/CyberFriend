"""A question about somebody's pools or loans goes to the chain, never the corpus.

The same failure the wallet route was written for, one step further: a channel
message about a liquidity pool is a record of what somebody said, and an
answer built from it would present a colleague's range as yours.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from chatmemory.app.egress import DEFI_POSITIONS_PROVIDER, closed_vocabulary_for
from chatmemory.app.reasoning.loop import ToolOutcome
from chatmemory.app.reasoning.ports import ExternalTool, ToolCall, ToolCompletion
from chatmemory.app.reasoning.service import EXTERNAL_ROUTE, WALLET_ADDRESS_MISSING
from chatmemory.app.routing import PositionKind, defi_question, wallet_question
from tests.unit.test_external_routing import WEB
from tests.unit.test_loop_invocation import ScriptedSurface
from tests.unit.test_market_routing import decision_outcomes
from tests.unit.test_reasoning_fixed import FakeRetrieval, question
from tests.unit.test_wallet_routing import (
    SOMEBODY_ELSES_PROJECT,
    WALLET,
    ParaphrasingSynthesizer,
    service,
)

ADDRESS = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"
SAVED = "0xdd8ac30cb0a219af4963eab5e30ed065b5e63d68"


def _tool(name: str) -> ExternalTool:
    return ExternalTool(
        qualified_name=f"{DEFI_POSITIONS_PROVIDER}:{name}",
        server=DEFI_POSITIONS_PROVIDER,
        description=name,
        input_schema={"type": "object", "properties": {"address": {"type": "string"}}},
    )


LIQUIDITY = _tool("liquidity_positions")
LENDING = _tool("lending_positions")
BOTH = _tool("defi_positions")
ALL = (WALLET, LIQUIDITY, LENDING, BOTH, WEB)

REPORT = (
    f"Liquidity positions for `{ADDRESS.lower()}`\n\n**Base**\n"
    "• Uniswap v3 #4558505 · WETH/USDC 0.05% · 🟢 in range\n"
    "  Range: 2,156.02 – 4,500.67 USDC per WETH · now 2,720.99"
)


def _surface(tool: ExternalTool, address: str = ADDRESS) -> ScriptedSurface:
    return ScriptedSurface(
        *ALL,
        completion=ToolCompletion(
            call=ToolCall(name=tool.qualified_name, arguments={"address": address}, call_id="d1"),
            prompt_tokens=20,
        ),
        outcome=ToolOutcome(
            invoked=True, source_system=DEFI_POSITIONS_PROVIDER, text=REPORT, attribution="chain"
        ),
    )


# --- recognising the question ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "address"),
    [
        (f"what are the liquidity pools of {ADDRESS}?", PositionKind.LIQUIDITY, True),
        ("show my LP positions", PositionKind.LIQUIDITY, False),
        ("quais são minhas posições de liquidez?", PositionKind.LIQUIDITY, False),
        ("are my uniswap v3 ranges in range?", PositionKind.LIQUIDITY, False),
        (f"aave positions for {ADDRESS}", PositionKind.LENDING, True),
        ("what's my health factor?", PositionKind.LENDING, False),
        ("how much did I borrow on aave?", PositionKind.LENDING, False),
        ("minhas posições no aave", PositionKind.LENDING, False),
        (f"defi positions {ADDRESS}", PositionKind.BOTH, True),
        ("check my uniswap and aave", PositionKind.BOTH, False),
    ],
)
def test_positions_questions_are_recognised(text: str, kind: PositionKind, address: bool) -> None:
    found = defi_question(text)
    assert found is not None
    assert found.kind is kind
    assert (found.address is not None) is address


@pytest.mark.parametrize(
    "text",
    [
        # Conversations about pools stay with the corpus.
        "what did we decide about the pool?",
        "what did people say about aave?",
        # Not crypto at all: the words alone must not be enough.
        "my loan from the bank",
        "my health insurance renewal",
        "should I join the pool party",
        "I think the pool is broken",
        # Balances are the wallet route's.
        "what is my wallet balance?",
    ],
)
def test_other_questions_are_not_positions_questions(text: str) -> None:
    assert defi_question(text) is None


def test_a_positions_question_with_an_address_is_also_a_wallet_question() -> None:
    """Which is why the positions route must be checked first."""
    text = f"liquidity pools of {ADDRESS}"
    assert wallet_question(text) is not None
    assert defi_question(text) is not None


def test_the_positions_provider_is_held_to_rooting() -> None:
    assert closed_vocabulary_for(DEFI_POSITIONS_PROVIDER) is None


# --- the route, through the real answer service ----------------------------


@pytest.mark.parametrize(
    ("text", "tool"),
    [
        (f"what are the liquidity pools of {ADDRESS}?", LIQUIDITY),
        (f"aave borrow and supply of {ADDRESS}", LENDING),
        (f"defi positions of {ADDRESS}", BOTH),
    ],
)
async def test_a_positions_question_never_reaches_the_corpus(
    text: str, tool: ExternalTool
) -> None:
    surface = _surface(tool)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(
        question(text=text)
    )

    assert retrieval.calls == [], "the corpus was searched for somebody's positions"
    # Exactly one tool offered: the route decided what is read, not the model.
    assert [names for _, names in surface.proposed] == [(tool.qualified_name,)]
    assert ADDRESS in surface.invocations[0].question
    assert "WETH/USDC" in outcome.answer.text
    assert "project" not in outcome.answer.text
    assert decision_outcomes(outcome, EXTERNAL_ROUTE)[0].startswith("defi_")


async def test_a_saved_wallet_is_used_when_none_is_written() -> None:
    surface = _surface(LIQUIDITY, SAVED)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])
    asked = replace(question(text="show my LP positions"), asker_values=frozenset({SAVED}))

    await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(asked)

    assert retrieval.calls == []
    assert SAVED in surface.invocations[0].question
    assert SAVED in surface.invocations[0].asker_values


async def test_without_any_wallet_an_address_is_asked_for() -> None:
    surface = _surface(LENDING)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(
        question(text="what's my health factor?")
    )

    assert retrieval.calls == []
    assert surface.invocations == []
    assert outcome.answer.text == WALLET_ADDRESS_MISSING
