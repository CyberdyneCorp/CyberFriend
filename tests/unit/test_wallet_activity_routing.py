"""A question about what a wallet did goes to the activity tool, never the corpus.

Both directions, both languages: "o que essa carteira fez essa semana?" is
activity, and "what did people say about my wallet" stays a question about the
conversation. And the window is the asker's words, never the model's: the tool
takes an address and nothing else, and the provider reads the span from the
question the clearance carries.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from chatmemory.adapters.chain.clearance import Cleared, clear_address
from chatmemory.adapters.chain.deployments import DEPLOYMENTS
from chatmemory.adapters.chain.positions_provider import ACTIVITY_TOOL, PositionsProvider
from chatmemory.adapters.chain.registration import ChainToolsConfig, build_chain_tools
from chatmemory.adapters.market.registration import MarketToolsConfig, build_market_tools
from chatmemory.adapters.mcp_client.config import FederationConfig
from chatmemory.adapters.mcp_client.registry import ServerDiscovery, register
from chatmemory.adapters.mcp_client.routing import ToolRouter
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.registration import WebToolsConfig, build_web_tools
from chatmemory.app.egress import (
    DEFI_POSITIONS_PROVIDER,
    AuthorizedQuery,
    EgressGuard,
    EgressRequest,
    ProvenancedQuery,
    QueryOrigin,
    authorized,
)
from chatmemory.app.reasoning.loop import ToolOutcome
from chatmemory.app.reasoning.ports import ExternalTool, ToolCall, ToolCompletion
from chatmemory.app.reasoning.service import (
    ACTIVITY_TOOL as QUALIFIED_ACTIVITY_TOOL,
)
from chatmemory.app.reasoning.service import (
    EXTERNAL_ROUTE,
    WALLET_ADDRESS_MISSING,
    WHICH_SAVED_WALLET,
)
from chatmemory.app.routing_crypto import CryptoRoute, crypto_route
from chatmemory.app.wallet_activity import activity_question
from chatmemory.config import Settings
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import PersonRef
from tests.unit.test_defi_routing import ALL, SAVED
from tests.unit.test_loop_invocation import ScriptedSurface
from tests.unit.test_market_routing import decision_outcomes, remembering
from tests.unit.test_reasoning_fixed import FakeRetrieval, question
from tests.unit.test_wallet_routing import (
    SOMEBODY_ELSES_PROJECT,
    ParaphrasingSynthesizer,
    service,
)

ADDRESS = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"
SECOND = "0x1111111111111111111111111111111111114a2b"

ACTIVITY = ExternalTool(
    qualified_name=QUALIFIED_ACTIVITY_TOOL,
    server=DEFI_POSITIONS_PROVIDER,
    description="wallet activity",
    input_schema={"type": "object", "properties": {"address": {"type": "string"}}},
)

REPORT = (
    f"Activity of `{SAVED}` — 17 Sep 2026 00:00 – 24 Sep 2026 14:05 UTC\n"
    "_Period: 17 Sep 2026 00:00 – 24 Sep 2026 14:05 UTC._\n\n**Base** — 1 action\n"
    "`18 Sep 16:22` swapped 0.004 ETH → 10.28 USDC (≈ $10.28)"
)


def _surface(address: str = SAVED) -> ScriptedSurface:
    return ScriptedSurface(
        *ALL,
        ACTIVITY,
        completion=ToolCompletion(
            call=ToolCall(name=QUALIFIED_ACTIVITY_TOOL, arguments={"address": address},
                          call_id="a1"),
            prompt_tokens=20,
        ),
        outcome=ToolOutcome(
            invoked=True, source_system=DEFI_POSITIONS_PROVIDER, text=REPORT, attribution="chain"
        ),
    )


# --- recognising the question ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "o que essa carteira fez essa semana?",
        "o que minha carteira fez nos últimos 30 dias?",
        "what did this wallet do this week?",
        "show my wallet activity",
        "minhas transações de ontem",
        "my wallet history for the last 7 days",
        "histórico da minha carteira",
        "what has my wallet done this month?",
        "movimentações da minha carteira hoje",
    ],
)
def test_activity_about_my_wallet_is_recognised_in_both_languages(text: str) -> None:
    found = crypto_route(text)
    assert found is not None
    assert found.route is CryptoRoute.WALLET_ACTIVITY
    assert found.mine and found.addresses == ()


@pytest.mark.parametrize(
    "text",
    [f"what did {ADDRESS} do this week?", f"o que {ADDRESS} fez ontem?",
     f"transactions of {ADDRESS}"],
)
def test_a_typed_address_is_the_one_read(text: str) -> None:
    found = crypto_route(text)
    assert found is not None
    assert found.route is CryptoRoute.WALLET_ACTIVITY
    assert found.addresses == (ADDRESS.lower(),) and not found.mine


@pytest.mark.parametrize(
    "text",
    [
        # About the conversation, with a wallet in it.
        "what did people say about my wallet",
        "o que o pessoal disse sobre a minha carteira essa semana?",
        f"what did people say about {ADDRESS}?",
        # Somebody's week, not a wallet's.
        "what did John do this week?",
        "o que o João fez ontem?",
        # Activity of something that is not a wallet.
        "my activity this week",
        "what's the history of the deploy?",
    ],
)
def test_other_questions_are_not_activity(text: str) -> None:
    found = crypto_route(text)
    assert found is None or found.route is not CryptoRoute.WALLET_ACTIVITY


@pytest.mark.parametrize(
    ("text", "route"),
    [
        ("quanto eu tenho no total?", CryptoRoute.PORTFOLIO),
        ("what's my balance?", CryptoRoute.WALLET_BALANCE),
        ("show my pools", CryptoRoute.DEFI_LIQUIDITY),
        ("what did my wallet do this week?", CryptoRoute.WALLET_ACTIVITY),
    ],
)
def test_activity_takes_nothing_from_the_other_chain_routes(text: str, route: CryptoRoute) -> None:
    found = crypto_route(text)
    assert found is not None and found.route is route


def test_what_did_it_do_after_a_balance_question_is_that_address() -> None:
    found = activity_question("what did it do this week?", (f"what does {ADDRESS} hold?",))
    assert found is not None
    assert found.address == ADDRESS.lower() and found.carried


def test_and_last_month_after_an_activity_question_is_activity_again() -> None:
    found = crypto_route("and last month?", ("what did my wallet do this week?",))
    assert found is not None and found.route is CryptoRoute.WALLET_ACTIVITY
    assert found.mine


def test_and_last_month_after_anything_else_is_not_activity() -> None:
    assert activity_question("and last month?", ("when is the deploy?",)) is None


# --- the route, through the real answer service ---------------------------------------


async def test_a_dm_question_with_a_saved_wallet_reads_it_and_never_the_corpus() -> None:
    surface = _surface()
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])
    asked = replace(
        question(text="o que minha carteira fez essa semana?"), asker_values=frozenset({SAVED})
    )

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(asked)

    assert retrieval.calls == [], "the corpus was searched for a wallet's history"
    assert [names for _, names in surface.proposed] == [(QUALIFIED_ACTIVITY_TOOL,)]
    request = surface.invocations[0]
    assert SAVED in request.question and SAVED in request.asker_values
    assert request.private, "a DM is private: the provider may name counterparties"
    assert "swapped 0.004 ETH → 10.28 USDC" in outcome.answer.text
    assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["wallet_activity"]


async def test_a_channel_question_is_not_private() -> None:
    surface = _surface()
    asked = replace(
        question(text="what did my wallet do this week?"),
        asker_values=frozenset({SAVED}),
        audience=Audience(
            mode=DeliveryMode.PUBLIC_CHANNEL, members=frozenset(), readable_channels=frozenset()
        ),
    )

    await service(surface, FakeRetrieval([[]]), ParaphrasingSynthesizer()).answer_run(asked)

    assert not surface.invocations[0].private


async def test_several_saved_wallets_and_none_named_asks_which() -> None:
    surface = _surface()
    asked = replace(
        question(text="what did my wallet do this week?"),
        asker_values=frozenset({SAVED, SECOND}),
    )

    outcome = await service(surface, FakeRetrieval([[]]), ParaphrasingSynthesizer()).answer_run(
        asked
    )

    assert surface.invocations == []
    assert outcome.answer.text.startswith(WHICH_SAVED_WALLET)
    assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["wallet_choose"]


async def test_without_any_wallet_an_address_is_asked_for() -> None:
    surface = _surface()
    outcome = await service(surface, FakeRetrieval([[]]), ParaphrasingSynthesizer()).answer_run(
        question(text="show my wallet activity")
    )

    assert surface.invocations == []
    assert outcome.answer.text == WALLET_ADDRESS_MISSING


async def test_what_did_it_do_follows_the_address_asked_about_before() -> None:
    surface = _surface(ADDRESS)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])

    await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(
        remembering("what did it do this week?", f"what does {ADDRESS} hold?")
    )

    assert retrieval.calls == []
    assert ADDRESS.lower() in surface.invocations[0].question.lower()


# --- the provider: clearance, privacy and the window ---------------------------------

ASKER = PersonRef("discord", 1)


def _clearance(question_text: str, *, private: bool) -> AuthorizedQuery:
    return EgressGuard().authorize(
        EgressRequest(
            asker=ASKER,
            query=ProvenancedQuery(
                text=ADDRESS, origin=QueryOrigin.ASKER, question=question_text
            ),
            provider=DEFI_POSITIONS_PROVIDER,
            private=private,
        )
    )


@pytest.mark.parametrize("private", [True, False])
async def test_the_guard_carries_the_audience_to_the_clearance(private: bool) -> None:
    with authorized(_clearance(f"what did {ADDRESS} do?", private=private)):
        cleared = await clear_address(
            DEFI_POSITIONS_PROVIDER, ACTIVITY_TOOL, CallBudget(1), RateLimiter(0)
        )

    assert isinstance(cleared, Cleared)
    assert cleared.private is private


async def test_the_window_is_the_questions_and_never_the_models() -> None:
    """The model may add any argument it likes; the explorer is asked for the
    span the asker named, and only the address from the clearance."""
    asked: list[httpx.URL] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if "blockscout" in request.url.host:
            asked.append(request.url)
            return httpx.Response(200, json={"items": [], "next_page_params": None})
        return httpx.Response(500)

    provider = PositionsProvider(
        DEPLOYMENTS, "key", CallBudget(2), RateLimiter(0),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), timeout_seconds=2,
        clock=lambda: datetime(2026, 9, 24, 14, 5, tzinfo=UTC),
    )
    with authorized(_clearance(f"what did {ADDRESS} do yesterday?", private=True)):
        result = await provider.call_tool(
            ACTIVITY_TOOL, {"address": ADDRESS, "days": 365, "from": "2020-01-01"}
        )

    assert not result.is_error
    assert {u.params["age_from"] for u in asked} == {"2026-09-23T00:00:00Z"}
    assert {u.params["age_to"] for u in asked} == {"2026-09-24T00:00:00Z"}
    assert {u.params["from_address_hashes_to_include"] for u in asked} == {ADDRESS.lower()}
    assert "**Ethereum**, **Base**, **Arbitrum** — no activity" in result.text


# --- the tool router ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "what did my wallet do this week?",
        "what did this wallet do this week?",
        "show my wallet activity",
        "what has my wallet done this month?",
        "o que essa carteira fez essa semana?",
        "o que minha carteira fez nos últimos 30 dias?",
        "minhas transações de ontem",
    ],
)
async def test_the_tool_router_offers_activity_among_every_registered_tool(text: str) -> None:
    """Regression: with more read-only tools than a run may see, the router
    ranks by shared words, and "what did my wallet do this week?" tied with
    every wallet tool and lost -- the route then had nothing to offer and
    answered that the chain could not be read."""
    chain = build_chain_tools(ChainToolsConfig(infura_key="k"))
    market = build_market_tools(MarketToolsConfig(serpapi_key="k"))
    web = build_web_tools(WebToolsConfig(serpapi_key="k"))
    discoveries = [
        ServerDiscovery(name, tuple(await provider.list_tools()))
        for tools in (chain, market, web)
        for name, provider in tools.providers.items()
    ]
    config = FederationConfig(
        servers=(*chain.servers, *market.servers, *web.servers),
        allowlist=(*chain.allowlist, *market.allowlist, *web.allowlist),
    )
    limit = Settings.model_fields["federation_max_tools_per_run"].default
    assert len(config.allowlist) > limit, "the ranking is what this test is about"

    routed = ToolRouter(limit).route(text, register(config, discoveries))

    assert routed.offers(QUALIFIED_ACTIVITY_TOOL)
