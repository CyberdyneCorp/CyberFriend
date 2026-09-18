"""A wallet question goes to the chain, never to the corpus.

Written from a production failure. Asked for the balance of
`0xD5C9…bDd5`, the bot searched its channels, found a colleague describing a
*different* project -- "Coisas que voces podem fazer nele: 1. Calcular precos
das Cryptos 2. Saber informacoes do balanco da sua carteira" -- judged it
sufficient, and answered that it could not see the wallet but that "the
project can know balance information". The wallet tool was registered,
offered, read-only and never called.

The corpus answering first is the whole bug. A balance is not in it and cannot
be: a channel message about a wallet is a record of what somebody said. So the
route is decided before retrieval, exactly as it is for a current price.
"""

from __future__ import annotations

from collections.abc import Sequence

from chatmemory.app.egress import CHAIN_BALANCES_PROVIDER
from chatmemory.app.reasoning.budgets import Budget
from chatmemory.app.reasoning.contract import RunStatus
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.fixed import CorrectiveDriver
from chatmemory.app.reasoning.loop import ToolOutcome
from chatmemory.app.reasoning.ports import (
    ExternalTool,
    Grounded,
    PromptContext,
    ToolCall,
    ToolCompletion,
)
from chatmemory.app.reasoning.service import (
    EXTERNAL_ROUTE,
    WALLET_ADDRESS_MISSING,
    WALLET_UNAVAILABLE,
    ReasoningAnswerService,
    build_answer_service,
)
from chatmemory.domain.search import RelevanceSource
from tests.unit.test_composition import FakeChat
from tests.unit.test_external_routing import WEB
from tests.unit.test_loop_invocation import ScriptedSurface
from tests.unit.test_market_routing import decision_outcomes
from tests.unit.test_reasoning_fixed import (
    FakePlanner,
    FakeRetrieval,
    ScriptedCritic,
    ch,
    question,
)

ADDRESS = "0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5"

WALLET = ExternalTool(
    qualified_name="chain_balances:wallet_balances",
    server=CHAIN_BALANCES_PROVIDER,
    description="balances held by a wallet address on Ethereum and Base",
    input_schema={"type": "object", "properties": {"address": {"type": "string"}}},
)

WANTS_BALANCES = ToolCompletion(
    call=ToolCall(
        name=WALLET.qualified_name, arguments={"address": ADDRESS}, call_id="w1"
    ),
    prompt_tokens=20,
)

#: The message that actually answered in production, verbatim in substance.
SOMEBODY_ELSES_PROJECT = Evidence(
    window_id=21,
    channel=ch(100),
    text=(
        "Coisas que voces podem fazer nele: 1. Calcular precos das Cryptos "
        "2. Saber informacoes do balanco da sua carteira"
    ),
    score=0.9,
    relevance_source=RelevanceSource.FUSED_RRF,
    url="https://discord.com/channels/1/100/21",
    author_display="alguem",
    message_ids=(21,),
)

BALANCES = (
    "Balances for `0xd5c95af87f6e1e83507ac96b2ee4484b9afebdd5`\n\n"
    "**Ethereum**\n- 1.500000 ETH (~$3,750.00 at 2026-09-18 13:00Z)\n"
    "- 250.000000 USDC (~$250.00)\n\n"
    "**Base**\n- 0.200000 ETH (~$500.00 at 2026-09-18 13:00Z)"
)


def balances_outcome() -> ToolOutcome:
    return ToolOutcome(
        invoked=True,
        source_system=CHAIN_BALANCES_PROVIDER,
        text=BALANCES,
        attribution="Ethereum and Base",
    )


class ParaphrasingSynthesizer:
    """A model that would round the figures and drop their age."""

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(
        self, question_text: str, items: Sequence[Evidence], context: PromptContext
    ) -> Grounded:
        self.calls += 1
        return Grounded(
            text="the project can know balance information about your wallet",
            cited_window_ids=tuple(i.window_id for i in items),
            model_calls=1,
        )


def service(
    surface: ScriptedSurface,
    retrieval: FakeRetrieval,
    synthesizer: ParaphrasingSynthesizer,
) -> ReasoningAnswerService:
    return build_answer_service(
        retrieval,
        FakeChat(),
        planner=FakePlanner(),
        synthesizer=synthesizer,  # type: ignore[arg-type]
        fixed_driver=CorrectiveDriver(retrieval, ScriptedCritic(), budget=Budget()),
        loop_driver=CorrectiveDriver(retrieval, ScriptedCritic(), budget=Budget()),
        tools=surface,
    )


async def test_a_wallet_question_never_reaches_the_corpus() -> None:
    """The regression. Every assertion here failed in production."""
    surface = ScriptedSurface(
        WALLET, WEB, completion=WANTS_BALANCES, outcome=balances_outcome()
    )
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])
    synthesizer = ParaphrasingSynthesizer()

    outcome = await service(surface, retrieval, synthesizer).answer_run(
        question(text=f"qual o saldo da carteira {ADDRESS}?")
    )

    assert retrieval.calls == [], "the corpus was searched for a wallet balance"
    assert outcome.record.status is RunStatus.ANSWERED
    assert "1.500000 ETH" in outcome.answer.text
    assert "Calcular precos" not in outcome.answer.text
    assert "the project can know" not in outcome.answer.text
    assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["wallet"]
    # The adapter's own figures; no model paraphrases them.
    assert synthesizer.calls == 0
    # Clearance is minted from the asker's own words, which is what carries
    # the address out.
    assert ADDRESS in surface.invocations[0].question


async def test_a_bare_address_is_a_wallet_question() -> None:
    surface = ScriptedSurface(
        WALLET, WEB, completion=WANTS_BALANCES, outcome=balances_outcome()
    )
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(
        question(text=ADDRESS)
    )

    assert retrieval.calls == []
    assert "1.500000 ETH" in outcome.answer.text


async def test_a_wallet_question_with_no_address_asks_for_one() -> None:
    """"qual o balanco da carteira?" answered from whatever a colleague had
    written about wallets. No channel holds a live balance."""
    surface = ScriptedSurface(WALLET, WEB, completion=WANTS_BALANCES)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(
        question(text="qual o balanco da carteira?")
    )
    assert surface.invocations == [], "nothing to look up without an address"

    assert retrieval.calls == []
    assert outcome.answer.text == WALLET_ADDRESS_MISSING
    assert "0x" in outcome.answer.text, "the reply must say what to give"


async def test_an_unreachable_chain_is_not_reported_as_an_empty_wallet() -> None:
    """The one wrong answer somebody would act on."""
    surface = ScriptedSurface(
        WALLET,
        WEB,
        completion=WANTS_BALANCES,
        # The provider's own refusal: the chain could not be read, so nothing
        # was invoked and there is no figure to report.
        outcome=ToolOutcome(invoked=False, detail="could not be reached"),
    )
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(
        question(text=f"what does {ADDRESS} hold?")
    )

    assert retrieval.calls == []
    assert outcome.answer.text == WALLET_UNAVAILABLE
    assert "holds nothing" not in outcome.answer.text


async def test_a_question_about_what_people_said_still_reaches_the_corpus() -> None:
    """An address can be what a question is *about*. That one is the corpus's."""
    surface = ScriptedSurface(WALLET, WEB, completion=WANTS_BALANCES)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])

    await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(
        question(text=f"what did people say about {ADDRESS}")
    )

    assert retrieval.calls, "a question about the conversation must be retrieved"
