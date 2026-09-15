"""The web providers: what they send, what they return, and how they fail.

Every test here drives a real `httpx.AsyncClient` over a mock transport
rather than a hand-written fake provider. The parsing of a provider's JSON,
the query parameters that actually go on the wire, and the behaviour on a
500 are exactly the things a fake would get right by construction and the
real thing gets wrong, so the transport is the only part stood in for.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.provider import USER_AGENT
from chatmemory.adapters.web.query import web_arguments
from chatmemory.adapters.web.registration import (
    WebToolsConfig,
    build_web_tools,
)
from chatmemory.adapters.web.results import (
    NO_RESULTS_NOTE,
    SERPAPI_SERVER,
    TRUNCATION_NOTE,
    WIKIPEDIA_SERVER,
    WebResult,
    render,
    source_system_for,
)
from chatmemory.adapters.web.serpapi import SerpApiProvider
from chatmemory.adapters.web.wikipedia import WikipediaProvider
from chatmemory.app.authorization import CredentialScope, ToolEffect
from chatmemory.app.reasoning.evidence import SOURCE_DISCORD, SOURCE_WEB


@pytest.fixture(autouse=True)
def _clearance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the query these tests actually send, and nothing else.

    The provider reads its clearance out of band, because `arguments` is
    written by the model -- so checking one argument against another compared
    model output with model output. These tests exercise provider behaviour,
    so they stand in for the guard by clearing exactly the query a legitimate
    run would have cleared. A query the guard would refuse is still refused,
    which is what keeps the smuggling test meaningful.
    """
    from chatmemory.adapters.web import provider as provider_mod
    from chatmemory.app import egress
    from chatmemory.domain.identity import PersonRef

    def _granted(provider: str) -> egress.AuthorizedQuery:
        return egress.AuthorizedQuery(
            text=QUERY,
            provider=provider,
            asked_by=PersonRef("discord", 1),
            question=QUESTION,
            minted_by=egress._MINTED_BY_GUARD,
        )

    monkeypatch.setattr(provider_mod, "current_authorization", _granted)
def cleared(provider: str, question: str, query: str):
    """Clearance for one dispatch, minted the only way it can be.

    The provider no longer reads the asking question out of the arguments --
    those are written by the model, so checking one against the other compared
    model output with model output. Tests therefore go through the guard, which
    is also what production does.
    """
    from chatmemory.app.egress import (
        EgressGuard,
        EgressRequest,
        ProvenancedQuery,
        authorized,
    )
    from chatmemory.domain.identity import PersonRef

    return authorized(
        EgressGuard().authorize(
            EgressRequest(
                asker=PersonRef("discord", 1),
                query=ProvenancedQuery(
                    text=query, origin=ProvenancedQuery.from_asker(question).origin,
                    question=question,
                ),
                provider=provider,
            )
        )
    )

QUESTION = "what is the kubernetes control plane"
QUERY = "kubernetes control plane"

WIKI_SEARCH: Mapping[str, Any] = {
    "query": {
        "search": [
            {
                "title": "Kubernetes",
                "snippet": '<span class="searchmatch">Kubernetes</span> is an '
                "open-source container orchestration system &amp; more",
            },
            {"title": "Control plane", "snippet": "The control plane routes"},
        ]
    }
}

WIKI_SUMMARY: Mapping[str, Any] = {
    "query": {
        "pages": [
            {
                "index": 2,
                "title": "Control plane",
                "extract": "In networking the control plane decides routes.",
                "fullurl": "https://en.wikipedia.org/wiki/Control_plane",
            },
            {
                "index": 1,
                "title": "Kubernetes",
                "extract": "Kubernetes is an open-source container system.",
                "fullurl": "https://en.wikipedia.org/wiki/Kubernetes",
            },
        ]
    }
}

SERP: Mapping[str, Any] = {
    "organic_results": [
        {
            "title": "Kubernetes Components",
            "link": "https://kubernetes.io/docs/concepts/overview/components/",
            "snippet": "The control plane makes global decisions.",
        }
    ]
}


class FakeWeb:
    """A provider endpoint that can be made to misbehave in each way that matters."""

    def __init__(
        self,
        payload: object = None,
        *,
        status: int = 200,
        delay: float = 0.0,
        body: str | None = None,
    ) -> None:
        self.payload = payload
        self.status = status
        self.delay = delay
        self.body = body
        self.requests: list[httpx.Request] = []
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle),
            headers={"User-Agent": USER_AGENT},
        )

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.body is not None:
            return httpx.Response(self.status, text=self.body)
        return httpx.Response(self.status, text=json.dumps(self.payload))

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def params(self) -> dict[str, str]:
        return dict(self.last.url.params)


def wikipedia(fake: FakeWeb, **kwargs: Any) -> WikipediaProvider:
    return WikipediaProvider(
        CallBudget(kwargs.pop("calls", 10)),
        client=fake.client,
        limiter=RateLimiter(0.0),
        **kwargs,
    )


def serpapi(fake: FakeWeb, key: str = "secret-key", **kwargs: Any) -> SerpApiProvider:
    return SerpApiProvider(
        key,
        CallBudget(kwargs.pop("calls", 10)),
        client=fake.client,
        limiter=RateLimiter(0.0),
        **kwargs,
    )


# --- what goes on the wire ---------------------------------------------


async def test_wikipedia_search_sends_the_query_and_nothing_else() -> None:
    fake = FakeWeb(WIKI_SEARCH)
    await wikipedia(fake).call_tool("search", web_arguments(QUESTION, QUERY))

    params = fake.params()
    assert params["srsearch"] == QUERY
    # No channel, no message, no requester, and not the question either: the
    # asker's words are what the query is checked against, not extra payload.
    assert set(params) == {
        "action",
        "list",
        "srsearch",
        "srlimit",
        "srprop",
        "format",
        "formatversion",
    }


async def test_serpapi_sends_the_query_the_count_and_the_key() -> None:
    fake = FakeWeb(SERP)
    await serpapi(fake).call_tool("search", web_arguments(QUESTION, QUERY))

    params = fake.params()
    assert params["q"] == QUERY
    assert set(params) == {"engine", "q", "num", "api_key"}


async def test_the_user_agent_identifies_the_deployment_not_a_person() -> None:
    """One constant string, whoever asks. Wikimedia's policy needs the contact
    URL in it; nothing about the requester may join it."""
    fake = FakeWeb(WIKI_SEARCH)
    provider = wikipedia(fake)
    await provider.call_tool("search", web_arguments(QUESTION, QUERY))
    await provider.call_tool("search", web_arguments("who runs kubernetes here", "kubernetes"))

    agents = {r.headers["user-agent"] for r in fake.requests}
    assert agents == {USER_AGENT}
    assert "http" in USER_AGENT


# --- reading a provider's answer ----------------------------------------


async def test_wikipedia_search_results_carry_a_source_and_an_openable_url() -> None:
    fake = FakeWeb(WIKI_SEARCH)
    result = await wikipedia(fake).call_tool("search", web_arguments(QUESTION, QUERY))

    assert not result.is_error
    assert "source: Wikipedia" in result.text
    assert "url: https://en.wikipedia.org/wiki/Kubernetes" in result.text
    # The API's own markup and entities are stripped, so a fenced block holds
    # readable text rather than half-rendered HTML.
    assert "<span" not in result.text
    assert "&amp;" not in result.text
    assert "Kubernetes is an open-source" in result.text


async def test_wikipedia_summary_leads_with_the_best_matching_article() -> None:
    """The API returns generator pages in arbitrary order; rank is separate."""
    fake = FakeWeb(WIKI_SUMMARY)
    result = await wikipedia(fake).call_tool("summary", web_arguments(QUESTION, QUERY))

    assert result.text.index("Kubernetes is an open-source") < result.text.index(
        "In networking the control plane"
    )
    assert "exintro" in fake.params()


async def test_serpapi_results_carry_the_link_google_returned() -> None:
    fake = FakeWeb(SERP)
    result = await serpapi(fake).call_tool("search", web_arguments(QUESTION, QUERY))

    assert not result.is_error
    assert "https://kubernetes.io/docs/concepts/overview/components/" in result.text
    assert "source: Google" in result.text


async def test_an_empty_result_set_says_so_rather_than_returning_nothing() -> None:
    fake = FakeWeb({"organic_results": []})
    result = await serpapi(fake).call_tool("search", web_arguments(QUESTION, QUERY))

    assert not result.is_error
    assert NO_RESULTS_NOTE in result.text


# --- a provider that misbehaves -----------------------------------------


async def test_an_error_status_degrades_the_call_without_ending_the_session() -> None:
    fake = FakeWeb(SERP, status=500)
    provider = serpapi(fake)

    failed = await provider.call_tool("search", web_arguments(QUESTION, QUERY))
    assert failed.is_error

    fake.status = 200
    recovered = await provider.call_tool("search", web_arguments(QUESTION, QUERY))
    assert not recovered.is_error


async def test_nonsense_instead_of_json_degrades_rather_than_raising() -> None:
    fake = FakeWeb(body="<html>we have moved</html>")
    result = await wikipedia(fake).call_tool("search", web_arguments(QUESTION, QUERY))

    assert result.is_error


async def test_a_payload_shaped_differently_yields_no_results_not_a_crash() -> None:
    fake = FakeWeb({"query": {"search": "not a list"}})
    result = await wikipedia(fake).call_tool("search", web_arguments(QUESTION, QUERY))

    assert not result.is_error
    assert NO_RESULTS_NOTE in result.text


async def test_a_slow_provider_times_out_and_the_next_call_still_works() -> None:
    fake = FakeWeb(WIKI_SEARCH, delay=0.5)
    provider = wikipedia(fake, timeout_seconds=0.05)

    timed_out = await provider.call_tool("search", web_arguments(QUESTION, QUERY))
    assert timed_out.is_error

    fake.delay = 0.0
    assert not (await provider.call_tool("search", web_arguments(QUESTION, QUERY))).is_error


async def test_an_unknown_tool_name_is_refused() -> None:
    fake = FakeWeb(WIKI_SEARCH)
    result = await wikipedia(fake).call_tool("delete_page", web_arguments(QUESTION, QUERY))

    assert result.is_error
    assert not fake.requests


# --- the key never leaves -----------------------------------------------


async def test_a_failing_serpapi_call_never_reports_the_api_key() -> None:
    """httpx puts the request URL in an HTTPStatusError, and the key is in it."""
    fake = FakeWeb(SERP, status=401)
    result = await serpapi(fake, key="sk-live-do-not-leak").call_tool(
        "search", web_arguments(QUESTION, QUERY)
    )

    assert result.is_error
    assert "sk-live-do-not-leak" not in result.text


def test_redaction_covers_any_message_the_provider_logs() -> None:
    fake = FakeWeb(SERP)
    provider = serpapi(fake, key="sk-live-do-not-leak")

    scrubbed = provider.redact("GET https://serpapi.com/search.json?api_key=sk-live-do-not-leak")
    assert "sk-live" not in scrubbed
    assert "***" in scrubbed


# --- bounded results ----------------------------------------------------


async def test_a_large_result_set_is_cut_at_the_limit_and_says_so() -> None:
    payload = {
        "organic_results": [
            {"title": f"Result {i}", "link": f"https://example.test/{i}", "snippet": "x" * 400}
            for i in range(20)
        ]
    }
    fake = FakeWeb(payload)
    result = await serpapi(fake, max_result_chars=600, max_results=20).call_tool(
        "search", web_arguments(QUESTION, QUERY)
    )

    assert TRUNCATION_NOTE in result.text
    assert len(result.text) <= 600 + len(TRUNCATION_NOTE) + 1


async def test_more_results_than_asked_for_are_dropped() -> None:
    payload = {
        "organic_results": [
            {"title": f"Result {i}", "link": f"https://example.test/{i}", "snippet": "short"}
            for i in range(10)
        ]
    }
    fake = FakeWeb(payload)
    result = await serpapi(fake, max_results=2).call_tool(
        "search", web_arguments(QUESTION, QUERY)
    )

    assert "Result 1" in result.text
    assert "Result 2" not in result.text


def test_render_states_what_kind_of_evidence_this_is() -> None:
    text, truncated = render(
        "Wikipedia",
        QUERY,
        (WebResult("Kubernetes", "https://example.test", "orchestration", "Wikipedia"),),
        4000,
    )

    assert not truncated
    assert "external internet content" in text
    assert "not from this server's conversations" in text


# --- rate limiting and the per-run cap -----------------------------------


async def test_calls_are_spaced_by_the_rate_limiter() -> None:
    clock = [0.0]
    limiter = RateLimiter(min_interval=10.0, max_wait=0.0, clock=lambda: clock[0])

    assert await limiter.acquire()
    # The second caller would have to wait ten seconds, which is longer than
    # an answer can wait, so it is refused rather than queued.
    assert not await limiter.acquire()

    clock[0] = 10.0
    assert await limiter.acquire()


async def test_a_rate_limited_provider_degrades_the_call() -> None:
    fake = FakeWeb(WIKI_SEARCH)
    provider = wikipedia(fake)
    provider._limiter = RateLimiter(min_interval=30.0, max_wait=0.0)  # noqa: SLF001

    assert not (await provider.call_tool("search", web_arguments(QUESTION, QUERY))).is_error
    limited = await provider.call_tool("search", web_arguments(QUESTION, QUERY))
    assert limited.is_error
    assert len(fake.requests) == 1


async def test_one_question_cannot_cause_unbounded_outbound_calls() -> None:
    fake = FakeWeb(WIKI_SEARCH)
    provider = wikipedia(fake, calls=2)

    for _ in range(2):
        assert not (await provider.call_tool("search", web_arguments(QUESTION, QUERY))).is_error
    assert (await provider.call_tool("search", web_arguments(QUESTION, QUERY))).is_error
    assert len(fake.requests) == 2


async def test_the_budget_is_shared_across_providers_because_egress_is() -> None:
    wiki_fake, serp_fake = FakeWeb(WIKI_SEARCH), FakeWeb(SERP)
    budget = CallBudget(1)
    wiki = WikipediaProvider(budget, client=wiki_fake.client, limiter=RateLimiter(0.0))
    serp = SerpApiProvider("k", budget, client=serp_fake.client, limiter=RateLimiter(0.0))

    assert not (await wiki.call_tool("search", web_arguments(QUESTION, QUERY))).is_error
    assert (await serp.call_tool("search", web_arguments(QUESTION, QUERY))).is_error


async def test_a_different_question_gets_its_own_budget() -> None:
    fake = FakeWeb(WIKI_SEARCH)
    provider = wikipedia(fake, calls=1)

    await provider.call_tool("search", web_arguments(QUESTION, QUERY))
    other = "who maintains the ingress controller"
    assert not (
        await provider.call_tool("search", web_arguments(other, "ingress controller"))
    ).is_error


async def test_a_later_run_asking_the_same_thing_gets_its_own_budget() -> None:
    """Spend belongs to a run, and a run is seconds long.

    Without this, the second time anyone asks a popular question the agent
    would silently stop being able to search.
    """
    clock = [0.0]
    fake = FakeWeb(WIKI_SEARCH)
    budget = CallBudget(1, window_seconds=60.0, clock=lambda: clock[0])
    provider = WikipediaProvider(budget, client=fake.client, limiter=RateLimiter(0.0))

    assert not (await provider.call_tool("search", web_arguments(QUESTION, QUERY))).is_error
    assert (await provider.call_tool("search", web_arguments(QUESTION, QUERY))).is_error

    clock[0] = 61.0
    assert not (await provider.call_tool("search", web_arguments(QUESTION, QUERY))).is_error
    assert budget.spent_for(QUESTION) == 1


def test_a_budget_must_allow_at_least_one_call() -> None:
    with pytest.raises(ValueError, match="at least one call"):
        CallBudget(0)


# --- registration --------------------------------------------------------


def test_without_a_key_serpapi_is_absent_rather_than_present_and_failing() -> None:
    tools = build_web_tools(WebToolsConfig(serpapi_key=None))

    assert tools.server_names == (WIKIPEDIA_SERVER,)
    assert SERPAPI_SERVER not in tools.providers
    assert not [e for e in tools.allowlist if e.server == SERPAPI_SERVER]


def test_a_blank_key_counts_as_no_key() -> None:
    """A platform that renders every variable writes a blank for the unset ones."""
    assert build_web_tools(WebToolsConfig(serpapi_key="   ")).server_names == (WIKIPEDIA_SERVER,)


def test_with_a_key_serpapi_joins_the_same_allowlist() -> None:
    tools = build_web_tools(WebToolsConfig(serpapi_key="k"))

    assert set(tools.server_names) == {WIKIPEDIA_SERVER, SERPAPI_SERVER}
    assert {e.qualified_name for e in tools.allowlist} == {
        "wikipedia:search",
        "wikipedia:summary",
        "serpapi:search",
    }


def test_a_keyless_serpapi_provider_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        SerpApiProvider("", CallBudget(1))


def test_every_web_tool_is_declared_read_only_by_the_operator() -> None:
    for entry in build_web_tools(WebToolsConfig(serpapi_key="k")).allowlist:
        assert entry.effect is ToolEffect.READ_ONLY
        assert not entry.mutation_enabled
        assert entry.credential is CredentialScope.NARROW_READ_ONLY


def test_the_provider_gets_the_shorter_of_the_two_timeouts() -> None:
    """So a slow answer degrades as ours, with the connection intact."""
    tools = build_web_tools(WebToolsConfig(timeout_seconds=4.0))

    assert tools.servers[0].timeout_seconds > 4.0


# --- how an answer tells the two kinds apart -----------------------------


def test_a_web_tool_result_is_labelled_as_web_evidence() -> None:
    assert source_system_for("wikipedia:summary") == SOURCE_WEB
    assert source_system_for("serpapi:search") == SOURCE_WEB


def test_anything_that_is_not_a_web_tool_keeps_its_own_label() -> None:
    assert source_system_for("github:search_issues") is None
    assert SOURCE_WEB != SOURCE_DISCORD
