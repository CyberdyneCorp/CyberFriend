"""Google, through SerpApi, with a key that may simply not exist.

The absence rule is the interesting part. Without `SERPAPI_KEY` this provider
is not built, so the tool never reaches the registry and the reasoning loop
is never offered it. A present-but-failing tool would be worse than no tool:
the loop would plan around a capability it does not have, spend a round
finding out, and report "the search failed" for a deployment that never
bought a search in the first place.

The key is narrow and read-only and belongs to the deployment, not to the
person asking -- SerpApi returns the same public results whoever holds it, so
there is nothing a requester could borrow through it. It is also the one
secret in this package, and it travels as a query parameter because the API
takes no other form of auth. That is why `secret_values` exists: httpx puts
the full URL into the text of an HTTP status error, and an unscrubbed 401
would write the key into the log.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx

from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.provider import (
    DEFAULT_MAX_RESULT_CHARS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_TIMEOUT,
    WebProvider,
    WebToolSpec,
)
from chatmemory.adapters.web.results import (
    SERPAPI_SERVER,
    WebResult,
    as_mapping,
    as_sequence,
    as_text,
    result,
)

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

SEARCH = "search"

TOOLS = (
    WebToolSpec(
        name=SEARCH,
        description=(
            "Search the public web and internet with Google for current news, "
            "documentation, docs, a product, prices, an outage or outages, a "
            "release, releases, versions, errors, and anything latest, recent "
            "or outside this Discord server. Returns page titles, links and "
            "snippets."
        ),
    ),
)


class SerpApiError(Exception):
    """A provider failure whose message is safe to log.

    Carries a status and nothing else on purpose: the request URL holds the
    API key, so the exception that reports a bad request must not hold the
    request.
    """


class SerpApiProvider(WebProvider):
    """`ToolSession` over SerpApi's Google engine."""

    def __init__(
        self,
        api_key: str,
        budget: CallBudget,
        *,
        endpoint: str = SERPAPI_ENDPOINT,
        limiter: RateLimiter | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            # Constructing a keyless SerpApi provider is the mistake this
            # class exists to make impossible: it would register a tool that
            # cannot work. `build_web_tools` omits it instead.
            raise ValueError("SerpApiProvider requires an API key; omit the provider instead")
        super().__init__(
            server=SERPAPI_SERVER,
            endpoint=endpoint,
            tools=TOOLS,
            budget=budget,
            limiter=limiter,
            max_results=max_results,
            max_result_chars=max_result_chars,
            timeout_seconds=timeout_seconds,
            client=client,
            secret_values=(api_key,),
        )
        self._api_key = api_key

    async def fetch(
        self, tool: str, query: str, client: httpx.AsyncClient
    ) -> Sequence[WebResult]:
        payload = await self._get(client, query)
        organic = as_sequence(payload.get("organic_results"))
        return [self._from_organic(as_mapping(hit)) for hit in organic]

    async def _get(self, client: httpx.AsyncClient, query: str) -> Mapping[str, object]:
        response = await client.get(
            self.endpoint,
            params={
                "engine": "google",
                # The query, the result count, and the key. Nothing about the
                # channel it was asked in, the person who asked, or anything
                # the corpus holds.
                "q": query,
                "num": str(self._max_results),
                "api_key": self._api_key,
            },
            timeout=self._timeout,
        )
        if response.status_code != 200:
            # Not `raise_for_status`: its message embeds the request URL.
            raise SerpApiError(f"serpapi returned HTTP {response.status_code}")
        payload = as_mapping(response.json())
        reported = as_text(payload.get("error"))
        if reported:
            raise SerpApiError("serpapi reported an error for this query")
        return payload

    @staticmethod
    def _from_organic(hit: Mapping[str, object]) -> WebResult:
        built, _ = result(
            as_text(hit.get("title")),
            as_text(hit.get("link")),
            as_text(hit.get("snippet")),
            "Google",
        )
        return built
