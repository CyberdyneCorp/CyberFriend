"""A question about somebody's total goes to the portfolio tool, never the corpus.

Regression, from the live check that designed this route: "quanto eu tenho no
total?" and "what's my portfolio worth?" were both answered from retrieval,
because neither names a wallet nor a position, so no chain predicate claimed
them -- and a channel holds nobody's total.

Also here: the chain predicates behind one label and one precedence
(`routing_crypto`), and "what's my balance?" meaning the asker's saved wallet.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from chatmemory.app.egress import DEFI_POSITIONS_PROVIDER
from chatmemory.app.reasoning.loop import ToolOutcome
from chatmemory.app.reasoning.ports import ExternalTool, ToolCall, ToolCompletion
from chatmemory.app.reasoning.service import (
    EXTERNAL_ROUTE,
    PORTFOLIO_TOOL,
    WALLET_ADDRESS_MISSING,
)
from chatmemory.app.routing import portfolio_question, wallet_question
from chatmemory.app.routing_crypto import CryptoRoute, crypto_route
from tests.unit.test_defi_routing import ALL, SAVED
from tests.unit.test_external_routing import WEB
from tests.unit.test_loop_invocation import ScriptedSurface
from tests.unit.test_market_routing import decision_outcomes, remembering
from tests.unit.test_reasoning_fixed import FakeRetrieval, question
from tests.unit.test_wallet_routing import (
    SOMEBODY_ELSES_PROJECT,
    WALLET,
    WANTS_BALANCES,
    ParaphrasingSynthesizer,
    balances_outcome,
    service,
)

ADDRESS = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"

PORTFOLIO = ExternalTool(
    qualified_name=PORTFOLIO_TOOL,
    server=DEFI_POSITIONS_PROVIDER,
    description="portfolio total",
    input_schema={"type": "object", "properties": {"addresses": {"type": "string"}}},
)

REPORT = (
    f"Portfolio for …{ADDRESS.lower()[-4:]}\n\n**Base** — $19,686.36\n"
    "  Wallet $69.80 · 69.13 USDC\n\n**Total ≈ $19,756.16**"
)


def _surface(*addresses: str) -> ScriptedSurface:
    return ScriptedSurface(
        *ALL,
        PORTFOLIO,
        WEB,
        completion=ToolCompletion(
            call=ToolCall(
                name=PORTFOLIO_TOOL,
                arguments={"addresses": " ".join(addresses)},
                call_id="p1",
            ),
            prompt_tokens=20,
        ),
        outcome=ToolOutcome(
            invoked=True, source_system=DEFI_POSITIONS_PROVIDER, text=REPORT, attribution="chain"
        ),
    )


# --- recognising the question ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "quanto eu tenho no total?",
        "Quanto tenho?",
        "quanto vale minha carteira?",
        "qual o meu saldo total?",
        "qual o patrimônio total da minha carteira cripto?",
        "meu portfólio vale quanto?",
        "what is my portfolio worth?",
        "what's my net worth on chain?",
        "what's my total balance?",
        "what's my wallet's total balance",
        "how much do I have in total?",
        "what is the total value of my wallets?",
        # Asking to see it asks for the total, with no value word.
        "show my portfolio",
        "show me my crypto portfolio.",
        "me mostra o meu portfólio?",
    ],
)
def test_totals_about_my_own_money_are_portfolio_questions(text: str) -> None:
    found = portfolio_question(text)
    assert found is not None
    assert found.mine and found.addresses == ()


@pytest.mark.parametrize(
    "text",
    [
        f"what is {ADDRESS} worth in total?",
        f"portfolio of {ADDRESS}",
    ],
)
def test_a_typed_address_is_read_for_that_address_only(text: str) -> None:
    found = portfolio_question(text)
    assert found is not None
    assert found.addresses == (ADDRESS.lower(),)
    assert not found.mine


@pytest.mark.parametrize(
    "text",
    [
        # Generic Portuguese: a total of something else.
        "quanto eu tenho de férias no total?",
        # Somebody else's, or a conversation about one.
        "what's the team's portfolio strategy",
        "what did we say about the portfolio",
        "o que o pessoal disse sobre o meu portfólio?",
        # Mine, but not a figure.
        "what's my portfolio strategy",
        "my net worth plan for 2027 retirement",
        # A position total is the positions route's.
        "how much do I have in total in my LP",
        # No total at all.
        "what is my balance?",
        "total value locked in uniswap",
        # Showing something that is not the asker's holdings.
        "show my portfolio of designs to Ana",
        "show the team's portfolio",
    ],
)
def test_other_questions_are_not_portfolio_questions(text: str) -> None:
    assert portfolio_question(text) is None


def test_and_in_total_after_a_chain_question_carries_its_address() -> None:
    found = portfolio_question("and in total?", (f"what does {ADDRESS} hold?",))
    assert found is not None
    assert found.addresses == (ADDRESS.lower(),) and found.carried


def test_e_no_total_after_a_saved_wallet_question_is_about_the_saved_wallet() -> None:
    found = portfolio_question("e no total?", ("qual o saldo da minha carteira?",))
    assert found is not None
    assert found.mine and found.addresses == ()


def test_e_no_total_after_anything_else_is_not_a_portfolio_question() -> None:
    assert portfolio_question("e no total?", ("quando é o deploy?",)) is None
    assert portfolio_question("e no total?") is None


# --- one label, one precedence ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "route"),
    [
        ("quanto eu tenho no total?", CryptoRoute.PORTFOLIO),
        # Names a wallet and a balance; "total" makes it everything.
        ("what's my wallet's total balance", CryptoRoute.PORTFOLIO),
        (f"liquidity pools of {ADDRESS}", CryptoRoute.DEFI_LIQUIDITY),
        ("what's my health factor?", CryptoRoute.DEFI_LENDING),
        ("check my uniswap and aave", CryptoRoute.DEFI_BOTH),
        ("how much do I have in total in my LP", CryptoRoute.DEFI_LIQUIDITY),
        (f"what does {ADDRESS} hold?", CryptoRoute.WALLET_BALANCE),
        ("what is my wallet balance?", CryptoRoute.WALLET_BALANCE),
        # The routing plan: first person and a balance means the saved wallet.
        ("what is my balance?", CryptoRoute.WALLET_BALANCE),
        ("qual o meu saldo?", CryptoRoute.WALLET_BALANCE),
    ],
)
def test_each_chain_question_gets_exactly_one_label(text: str, route: CryptoRoute) -> None:
    found = crypto_route(text)
    assert found is not None
    assert found.route is route


@pytest.mark.parametrize(
    "text",
    [
        "what did we decide about the pool?",
        "my balance of vacation days",
        "qual o saldo das minhas férias?",
        "quanto eu tenho de férias no total?",
        # No possessive: nobody's wallet, so the corpus answers.
        "qual o saldo?",
        "qual saldo?",
        "qual é o saldo atual?",
        "qual o saldo total?",
        "qual o valor total?",
    ],
)
def test_questions_about_anything_else_get_no_chain_label(text: str) -> None:
    assert crypto_route(text) is None


def test_what_is_my_balance_uses_the_saved_wallet() -> None:
    found = wallet_question("what's my balance?")
    assert found is not None and found.address is None


# --- the route, through the real answer service -------------------------------


async def test_a_portfolio_question_never_reaches_the_corpus() -> None:
    surface = _surface(SAVED)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])
    asked = replace(question(text="quanto eu tenho no total?"), asker_values=frozenset({SAVED}))

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(asked)

    assert retrieval.calls == [], "the corpus was searched for somebody's total"
    # Exactly one tool offered: the route decided what is read.
    assert [names for _, names in surface.proposed] == [(PORTFOLIO_TOOL,)]
    assert SAVED in surface.invocations[0].question
    assert SAVED in surface.invocations[0].asker_values
    assert "**Total ≈ $19,756.16**" in outcome.answer.text
    assert "project" not in outcome.answer.text
    assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["portfolio"]


async def test_the_saved_wallet_and_a_typed_one_are_summed_together() -> None:
    surface = _surface(SAVED, ADDRESS)
    asked = replace(
        question(text=f"what's my portfolio worth with {ADDRESS} too?"),
        asker_values=frozenset({SAVED}),
    )

    await service(surface, FakeRetrieval([[]]), ParaphrasingSynthesizer()).answer_run(asked)

    spelled = surface.invocations[0].question
    assert SAVED in spelled and ADDRESS in spelled


async def test_somebody_elses_address_does_not_bring_the_saved_wallet_along() -> None:
    surface = _surface(ADDRESS)
    asked = replace(
        question(text=f"what is {ADDRESS} worth in total?"), asker_values=frozenset({SAVED})
    )

    await service(surface, FakeRetrieval([[]]), ParaphrasingSynthesizer()).answer_run(asked)

    assert SAVED not in surface.invocations[0].question


async def test_without_any_wallet_an_address_is_asked_for() -> None:
    surface = _surface(SAVED)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(
        question(text="what's my net worth on chain?")
    )

    assert retrieval.calls == []
    assert surface.invocations == []
    assert outcome.answer.text == WALLET_ADDRESS_MISSING
    assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["wallet_address_missing"]


async def test_and_in_total_follows_the_address_asked_about_before() -> None:
    surface = _surface(ADDRESS)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])

    await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(
        remembering("and in total?", f"what does {ADDRESS} hold?")
    )

    assert retrieval.calls == []
    assert ADDRESS.lower() in surface.invocations[0].question.lower()


async def test_what_is_my_balance_reads_the_saved_wallet_and_not_the_corpus() -> None:
    """The routing plan's reading: a bare "what's my balance?" is about the
    asker's own wallet. It used to stay with the corpus, which holds nobody's
    balance."""
    surface = ScriptedSurface(WALLET, WEB, completion=WANTS_BALANCES, outcome=balances_outcome())
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])
    asked = replace(question(text="what is my balance?"), asker_values=frozenset({SAVED}))

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(asked)

    assert retrieval.calls == []
    assert SAVED in surface.invocations[0].question
    assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["wallet"]
