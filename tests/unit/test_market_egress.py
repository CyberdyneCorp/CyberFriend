"""The closed-vocabulary clearance path, and the governed door around it.

The egress guard admits a web query only if every word came from the asker.
For market data that rule is replaced -- for the listed providers and nothing
else -- by membership in a fixed set. These tests hold both halves of that:
the replacement actually lets "ETH" out when someone asked about ether, and it
is not a hole: text outside the set is refused outright, never trimmed, and
free-text tools still require rooting exactly as before.

They drive `connect()` and `GuardedInvoker` with the real guard, because the
clearance is minted by the invoker from the request's question, and a test
that minted it by hand would not show the running path works.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from chatmemory.adapters.market.registration import MarketToolsConfig, build_market_tools
from chatmemory.adapters.mcp_client.client import connect
from chatmemory.adapters.mcp_client.config import FederationConfig
from chatmemory.adapters.mcp_client.invoker import GuardedInvoker, InvocationOutcome
from chatmemory.adapters.mcp_client.routing import RoutedTools
from chatmemory.adapters.web.query import web_arguments
from chatmemory.adapters.web.registration import WebToolsConfig, build_web_tools
from chatmemory.app.audit import AuditOutcome, InMemoryAuditTrail
from chatmemory.app.authorization import (
    ActionOrigin,
    Authorizer,
    ConfirmationLedger,
    InvocationRequest,
    Refusal,
)
from chatmemory.app.egress import (
    CLOSED_VOCABULARIES,
    MARKET_CRYPTO_PROVIDER,
    MARKET_FX_PROVIDER,
    ClosedVocabulary,
    EgressGuard,
    EgressRecord,
    EgressRefused,
    EgressRequest,
    ProvenancedQuery,
    QueryOrigin,
    RefusalReason,
    refusal_for,
)
from chatmemory.domain.identity import PersonRef
from tests.unit.test_market_tools import COINGECKO_OK, FRANKFURTER_OK, FakeMarket, answering

ALICE = PersonRef("discord", 1001)


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[EgressRecord] = []

    def record(self, entry: EgressRecord) -> None:
        self.entries.append(entry)


def reformulation(text: str, question: str) -> ProvenancedQuery:
    return ProvenancedQuery(text=text, origin=QueryOrigin.MODEL_REFORMULATION, question=question)


# --- the guard: membership for the listed providers only -------------------


def test_eth_is_cleared_for_crypto_although_the_asker_wrote_ether() -> None:
    """Task 5.9: "ETH" resolves and is not refused by the guard."""
    audit = RecordingAudit()
    clearance = EgressGuard(audit).authorize(
        EgressRequest(
            asker=ALICE,
            query=reformulation("ETH", "how much is ether worth right now?"),
            provider=MARKET_CRYPTO_PROVIDER,
        )
    )

    assert clearance.text == "ETH"
    assert audit.entries[-1].allowed
    assert audit.entries[-1].closed_vocabulary


def test_the_same_unrooted_word_is_still_refused_for_a_free_text_provider() -> None:
    """The extension is scoped: rooting still governs every other provider."""
    question = "how much is ether worth right now?"
    with pytest.raises(EgressRefused) as refused:
        EgressGuard(RecordingAudit()).authorize(
            EgressRequest(asker=ALICE, query=reformulation("ETH", question), provider="serpapi")
        )
    assert refused.value.reason is RefusalReason.NOT_ROOTED_IN_QUESTION


@pytest.mark.parametrize(
    ("provider", "text"),
    [
        (MARKET_FX_PROVIDER, "USD Q3"),
        (MARKET_FX_PROVIDER, "USD BRL EUR"),
        (MARKET_FX_PROVIDER, "USD;"),
        (MARKET_FX_PROVIDER, "USD-BRL"),
        (MARKET_FX_PROVIDER, "salary bands"),
        (MARKET_CRYPTO_PROVIDER, "BTC ETH"),
        (MARKET_CRYPTO_PROVIDER, "DOGE"),
        (MARKET_CRYPTO_PROVIDER, "USD"),
    ],
)
def test_anything_outside_the_set_is_refused_outright(provider: str, text: str) -> None:
    audit = RecordingAudit()
    # Every word here is in the question, so rooting would have admitted it.
    question = f"please look up {text}"
    with pytest.raises(EgressRefused) as refused:
        EgressGuard(audit).authorize(
            EgressRequest(asker=ALICE, query=reformulation(text, question), provider=provider)
        )

    assert refused.value.reason is RefusalReason.OUTSIDE_CLOSED_VOCABULARY
    assert not audit.entries[-1].allowed


def test_content_derived_text_is_refused_even_when_it_is_a_member() -> None:
    """Membership replaces rooting, not the origin gate."""
    query = ProvenancedQuery(
        text="BTC", origin=QueryOrigin.RETRIEVED_CONTENT, question="what is BTC at"
    )
    assert refusal_for(query, CLOSED_VOCABULARIES[MARKET_CRYPTO_PROVIDER]) is (
        RefusalReason.CONTENT_DERIVED
    )


def test_a_vocabulary_caps_how_many_members_one_call_carries() -> None:
    vocabulary = ClosedVocabulary(frozenset({"A", "B"}), max_terms=1)
    assert vocabulary.admits("a")
    assert not vocabulary.admits("A B")
    assert not vocabulary.admits("")
    with pytest.raises(ValueError):
        ClosedVocabulary(frozenset(), max_terms=1)


def test_only_market_providers_are_held_to_membership() -> None:
    assert set(CLOSED_VOCABULARIES) == {"market_crypto", "market_fx", "market_index"}


# --- the governed path, end to end ------------------------------------------


async def stack(
    fake: FakeMarket, **kwargs: Any
) -> tuple[GuardedInvoker, RoutedTools, InMemoryAuditTrail]:
    config = MarketToolsConfig(min_interval_seconds=0, **kwargs)
    tools = build_market_tools(config, client=fake.client)
    federation = await connect(tools.merge_into(FederationConfig()), tools.factory())
    confirmations = ConfirmationLedger()
    audit = InMemoryAuditTrail()
    invoker = GuardedInvoker(
        federation, Authorizer(federation.permits, confirmations), audit, confirmations
    )
    routed = RoutedTools(question="", tools=federation.registration.tools)
    return invoker, routed, audit


async def invoke(
    fake: FakeMarket, question: str, tool: str, arguments: Mapping[str, object]
) -> tuple[InvocationOutcome, InMemoryAuditTrail]:
    invoker, routed, audit = await stack(fake)
    request = InvocationRequest(
        requester=ALICE,
        question=question,
        qualified_name=tool,
        arguments=arguments,
        origin=ActionOrigin.REQUESTER_REQUEST,
    )
    return await invoker.invoke(request, routed), audit


async def test_asked_about_ether_the_eth_lookup_runs_through_the_invoker() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    outcome, audit = await invoke(
        fake, "what is the price of ether today?", "market_crypto:crypto_price", {"asset": "ETH"}
    )

    assert outcome.invoked, outcome.notice()
    assert outcome.result is not None
    assert "2,388.82 USD" in outcome.result.text
    assert "Ether (ETH)" in outcome.result.text
    assert audit.entries()[-1].outcome is AuditOutcome.INVOKED


async def test_a_conversion_in_words_the_asker_never_typed_as_codes_runs() -> None:
    fake = FakeMarket(answering(FRANKFURTER_OK))
    outcome, _ = await invoke(
        fake,
        "how many reais do I get for 100 dollars?",
        "market_fx:convert",
        {"amount": 100, "from": "USD", "to": "BRL"},
    )

    assert outcome.invoked, outcome.notice()
    assert outcome.result is not None
    assert "100 USD = 514.59 BRL" in outcome.result.text


@pytest.mark.parametrize(
    "to",
    [
        "BRL and the Q3 renewal numbers",
        "acme salary bands",
        "BRL salary",
        "brl.",
    ],
)
async def test_free_text_in_a_currency_is_refused_before_any_request(to: str) -> None:
    """Task 5.10, through the running path.

    Every word is also written into the question, so a rooting check would
    have admitted -- or trimmed down to -- a sendable query. Membership does
    neither: the call is refused and nothing leaves.
    """
    fake = FakeMarket(answering(FRANKFURTER_OK))
    outcome, audit = await invoke(
        fake,
        f"convert 100 USD to {to}",
        "market_fx:convert",
        {"amount": 100, "from": "USD", "to": to},
    )

    assert not outcome.invoked
    assert not fake.requests
    assert audit.entries()[-1].outcome is AuditOutcome.REFUSED


async def test_an_amount_sent_as_text_is_refused_not_trimmed() -> None:
    fake = FakeMarket(answering(FRANKFURTER_OK))
    outcome, _ = await invoke(
        fake,
        "convert 100 USD to BRL",
        "market_fx:convert",
        {"amount": "100", "from": "USD", "to": "BRL"},
    )

    assert not outcome.invoked
    assert not fake.requests


async def test_a_lookup_proposed_while_reading_a_message_never_leaves() -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    invoker, routed, _ = await stack(fake)
    outcome = await invoker.invoke(
        InvocationRequest(
            requester=ALICE,
            question="what is BTC at",
            qualified_name="market_crypto:crypto_price",
            arguments={"asset": "BTC"},
            origin=ActionOrigin.RETRIEVED_CONTENT,
        ),
        routed,
    )

    assert outcome.decision.refusal is Refusal.NOT_REQUESTER_ORIGIN
    assert not fake.requests


async def test_web_search_beside_market_tools_still_requires_rooting() -> None:
    """Both registered in one federation: membership does not leak across."""
    fake = FakeMarket(answering({"query": {"search": []}}))
    web = build_web_tools(WebToolsConfig(), client=fake.client)
    market = build_market_tools(MarketToolsConfig(), client=fake.client)
    config = market.merge_into(web.merge_into(FederationConfig()))
    federation = await connect(config, market.factory(web.factory()))
    confirmations = ConfirmationLedger()
    invoker = GuardedInvoker(
        federation,
        Authorizer(federation.permits, confirmations),
        InMemoryAuditTrail(),
        confirmations,
    )
    question = "what is the price of ether"
    outcome = await invoker.invoke(
        InvocationRequest(
            requester=ALICE,
            question=question,
            qualified_name="wikipedia:search",
            arguments=web_arguments(question, "USD BRL"),
            origin=ActionOrigin.REQUESTER_REQUEST,
        ),
        RoutedTools(question=question, tools=federation.registration.tools),
    )

    assert not outcome.invoked
    assert not fake.requests
