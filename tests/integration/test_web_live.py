"""The web providers against the real internet.

The unit suite proves the boundary logic against a mock transport, which is
where the query check, the budget and the degradation paths belong. What it
cannot prove is that the request we build is one Wikipedia actually answers
and that the JSON it sends back is shaped the way the parser believes. Those
are the two things that break when a provider changes its API, and they only
break in production.

Skipped rather than failed when the network is unreachable: an offline laptop
is an environment problem, not a defect. SerpApi additionally needs a key,
which is read from the environment and never printed, asserted on, or written
anywhere -- only its presence decides whether the test runs.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import httpx
import pytest
import pytest_asyncio

from chatmemory.adapters.mcp_client.client import connect
from chatmemory.adapters.mcp_client.config import FederationConfig
from chatmemory.adapters.mcp_client.invoker import GuardedInvoker
from chatmemory.adapters.mcp_client.routing import RoutedTools
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.query import web_arguments
from chatmemory.adapters.web.registration import WebToolsConfig, build_web_tools
from chatmemory.adapters.web.results import NO_RESULTS_NOTE
from chatmemory.adapters.web.serpapi import SerpApiProvider
from chatmemory.adapters.web.wikipedia import WIKIPEDIA_ENDPOINT, WikipediaProvider
from chatmemory.app.audit import AuditOutcome, InMemoryAuditTrail
from chatmemory.app.authorization import (
    ActionOrigin,
    Authorizer,
    ConfirmationLedger,
    InvocationRequest,
)
from chatmemory.app.egress import (
    EgressGuard,
    EgressRequest,
    ProvenancedQuery,
    QueryOrigin,
    authorized,
)
from chatmemory.domain.identity import PersonRef

pytestmark = pytest.mark.asyncio

ALICE = PersonRef("discord", 9001)


@contextmanager
def cleared(provider: str, question: str, query: str) -> Iterator[None]:
    """Real clearance from the real guard.

    These tests are about the whole governed path, so they go through
    EgressGuard rather than standing in for it: an integration test that
    stubs the boundary it is meant to exercise proves only that the stub
    works.
    """
    with authorized(
        EgressGuard().authorize(
            EgressRequest(
                asker=PersonRef("discord", 1),
                query=ProvenancedQuery(
                    text=query, origin=QueryOrigin.ASKER, question=question
                ),
                provider=provider,
            )
        )
    ):
        yield


QUESTION = "what is the python programming language used for"
QUERY = "python programming language"

TIMEOUT = 10.0


@pytest_asyncio.fixture
async def online() -> AsyncIterator[None]:
    """Skip the whole file when the network is not reachable."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(WIKIPEDIA_ENDPOINT, params={"action": "query", "format": "json"})
    except httpx.HTTPError as exc:
        pytest.skip(f"no network access: {type(exc).__name__}")
    yield


@pytest.fixture
def serpapi_key() -> str:
    """Skip tests that would spend a search credit when no key is configured."""
    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key:
        pytest.skip("no SERPAPI_KEY configured")
    return key


def wikipedia() -> WikipediaProvider:
    return WikipediaProvider(
        CallBudget(8), limiter=RateLimiter(0.5), timeout_seconds=TIMEOUT
    )


# --- Wikipedia -----------------------------------------------------------


async def test_wikipedia_search_returns_articles_with_working_links(online: None) -> None:
    async with wikipedia().opened() as provider:
        with cleared("wikipedia", QUESTION, QUERY):
            result = await provider.call_tool("search", web_arguments(QUESTION, QUERY))

    assert not result.is_error
    assert NO_RESULTS_NOTE not in result.text
    assert "source: Wikipedia" in result.text
    assert "url: https://en.wikipedia.org/wiki/" in result.text
    # The API embeds its own markup in snippets; a fenced block must not.
    assert "<span" not in result.text


async def test_wikipedia_summary_returns_a_lead_extract(online: None) -> None:
    async with wikipedia().opened() as provider:
        with cleared("wikipedia", QUESTION, QUERY):
            result = await provider.call_tool("summary", web_arguments(QUESTION, QUERY))

    assert not result.is_error
    assert "Python" in result.text
    # A lead section, not a title list: the point of the summary tool is text
    # an answer can actually rest a claim on.
    assert len(result.text) > 300


async def test_a_wikipedia_result_stays_inside_its_size_budget(online: None) -> None:
    provider = WikipediaProvider(
        CallBudget(4), limiter=RateLimiter(0.5), max_result_chars=400, timeout_seconds=TIMEOUT
    )
    async with provider.opened() as opened:
        with cleared("wikipedia", QUESTION, QUERY):
            result = await opened.call_tool("summary", web_arguments(QUESTION, QUERY))

    assert not result.is_error
    assert len(result.text) < 600


async def test_a_query_the_asker_never_wrote_is_refused_before_any_request(
    online: None,
) -> None:
    """The boundary holds against the real endpoint, not only against a mock."""
    async with wikipedia().opened() as provider:
        result = await provider.call_tool(
            "search", web_arguments(QUESTION, "acme corp salary bands")
        )

    assert result.is_error


async def test_a_nonsense_query_reports_nothing_found_rather_than_inventing(
    online: None,
) -> None:
    asked = "what is qzzxwvlkjhgfdsapoiuytrewq"
    async with wikipedia().opened() as provider:
        with cleared("wikipedia", asked, "qzzxwvlkjhgfdsapoiuytrewq"):
            result = await provider.call_tool(
                "search", web_arguments(asked, "qzzxwvlkjhgfdsapoiuytrewq")
            )

    assert not result.is_error
    assert NO_RESULTS_NOTE in result.text


async def test_the_whole_governed_path_works_against_the_real_wikipedia(
    online: None,
) -> None:
    """Registration, routing, authorization, invocation and audit, end to end."""
    tools = build_web_tools(WebToolsConfig(timeout_seconds=TIMEOUT))
    federation = await connect(tools.merge_into(FederationConfig()), tools.factory())
    confirmations = ConfirmationLedger()
    audit = InMemoryAuditTrail()
    invoker = GuardedInvoker(
        federation, Authorizer(federation.permits, confirmations), audit, confirmations
    )

    try:
        outcome = await invoker.invoke(
            InvocationRequest(
                requester=ALICE,
                question=QUESTION,
                qualified_name="wikipedia:summary",
                arguments=web_arguments(QUESTION, QUERY),
                origin=ActionOrigin.REQUESTER_REQUEST,
            ),
            RoutedTools(question=QUESTION, tools=federation.registration.tools),
        )
    finally:
        await federation.aclose()

    assert outcome.invoked
    evidence = outcome.evidence()
    assert evidence is not None
    assert "not from this server's conversations" in evidence.body
    entry = audit.entries()[-1]
    assert entry.outcome is AuditOutcome.INVOKED
    assert entry.server == "wikipedia"
    assert audit.verify()


# --- SerpApi -------------------------------------------------------------


async def test_serpapi_returns_google_results_with_links(
    online: None, serpapi_key: str
) -> None:
    provider = SerpApiProvider(
        serpapi_key, CallBudget(2), limiter=RateLimiter(0.5), timeout_seconds=TIMEOUT
    )
    async with provider.opened() as opened:
        with cleared("wikipedia", QUESTION, QUERY):
            result = await opened.call_tool("search", web_arguments(QUESTION, QUERY))

    assert not result.is_error
    assert "source: Google" in result.text
    assert "url: http" in result.text
    assert serpapi_key not in result.text


async def test_serpapi_is_absent_from_the_registry_without_a_key(online: None) -> None:
    """The one thing about SerpApi that must hold whether or not a key exists."""
    tools = build_web_tools(WebToolsConfig(serpapi_key=None))
    federation = await connect(tools.merge_into(FederationConfig()), tools.factory())

    try:
        assert "serpapi:search" not in federation.registration.names
    finally:
        await federation.aclose()
