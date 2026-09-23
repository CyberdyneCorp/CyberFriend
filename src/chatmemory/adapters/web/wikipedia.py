"""Wikipedia, read-only, with no credential to hold.

Chosen as the always-available provider precisely because it needs no key:
there is nothing here that could be scoped wrongly, nothing a requester could
borrow, and nothing whose absence would make the tool half-work. A deployment
that has configured nothing still has one way to say "the internet says",
clearly separated from what the team said.

Two tools rather than one, because a person asks two different questions.
`search` is "what pages are there about this" -- several titles, several
links. `summary` is "tell me what this is" -- the lead extract of the best
match, which is the shape a factual answer can actually quote.

Both read the MediaWiki action API. The REST summary endpoint is friendlier
but takes an exact title, which would mean a second round trip and a title
guessed by the model; a search generator gets the same text in one call and
keeps the choice of article inside the query the asker's words justified.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import quote, urlsplit

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
    WIKIPEDIA_SERVER,
    WebResult,
    as_mapping,
    as_number,
    as_sequence,
    as_text,
    result,
)

WIKIPEDIA_ENDPOINT = "https://en.wikipedia.org/w/api.php"

SEARCH = "search"
SUMMARY = "summary"

# Extract length is capped by the API as well as locally: asking for less
# costs less to transfer and leaves less to throw away.
EXTRACT_CHARS = 1200
SUMMARY_CANDIDATES = 3

TOOLS = (
    WebToolSpec(
        name=SEARCH,
        description=(
            "Search Wikipedia encyclopedia articles on the public internet for "
            "background information, a definition, definitions, history, a "
            "person, people, places, companies, science and technology. "
            "Returns article titles, links and matching snippets."
        ),
    ),
    WebToolSpec(
        name=SUMMARY,
        description=(
            "Read the Wikipedia summary of a public topic from the internet: "
            "what something is, who someone is, an overview, a definition, "
            "the meaning or history of a term, with a link to the full "
            "encyclopedia article."
        ),
    ),
)


class WikipediaProvider(WebProvider):
    """`ToolSession` over the MediaWiki action API."""

    def __init__(
        self,
        budget: CallBudget,
        *,
        endpoint: str = WIKIPEDIA_ENDPOINT,
        limiter: RateLimiter | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            server=WIKIPEDIA_SERVER,
            endpoint=endpoint,
            tools=TOOLS,
            budget=budget,
            limiter=limiter,
            max_results=max_results,
            max_result_chars=max_result_chars,
            timeout_seconds=timeout_seconds,
            client=client,
            transport=transport,
        )
        split = urlsplit(endpoint)
        # Article links are built against the wiki the API belongs to, so a
        # deployment pointed at another language or a mirror still returns
        # URLs that resolve.
        self._article_base = f"{split.scheme}://{split.netloc}/wiki/"

    async def fetch(
        self, tool: str, query: str, client: httpx.AsyncClient
    ) -> Sequence[WebResult]:
        if tool == SUMMARY:
            return await self._summary(query, client)
        return await self._search(query, client)

    async def _search(self, query: str, client: httpx.AsyncClient) -> Sequence[WebResult]:
        payload = await self._get(
            client,
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": str(self._max_results),
                "srprop": "snippet",
                "format": "json",
                "formatversion": "2",
            },
        )
        hits = as_sequence(as_mapping(payload.get("query")).get("search"))
        return [self._from_search(as_mapping(hit)) for hit in hits]

    async def _summary(self, query: str, client: httpx.AsyncClient) -> Sequence[WebResult]:
        payload = await self._get(
            client,
            {
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrlimit": str(SUMMARY_CANDIDATES),
                "prop": "extracts|info",
                # The lead section only, as plain text: the full article is
                # megabytes and the lead is the part that answers "what is".
                "exintro": "1",
                "explaintext": "1",
                "exchars": str(EXTRACT_CHARS),
                "exlimit": "max",
                "inprop": "url",
                "format": "json",
                "formatversion": "2",
            },
        )
        pages = [as_mapping(p) for p in as_sequence(as_mapping(payload.get("query")).get("pages"))]
        # A generator returns pages in arbitrary order and states the search
        # rank separately; without this the "best match" would be whichever
        # page id happened to sort first.
        pages.sort(key=lambda p: as_number(p.get("index"), len(pages)))
        return [self._from_page(page) for page in pages]

    async def _get(
        self, client: httpx.AsyncClient, params: Mapping[str, str]
    ) -> Mapping[str, object]:
        response = await client.get(self.endpoint, params=dict(params), timeout=self._timeout)
        response.raise_for_status()
        return as_mapping(response.json())

    def _from_search(self, hit: Mapping[str, object]) -> WebResult:
        title = as_text(hit.get("title"))
        built, _ = result(title, self._article_url(title), as_text(hit.get("snippet")), "Wikipedia")
        return built

    def _from_page(self, page: Mapping[str, object]) -> WebResult:
        title = as_text(page.get("title"))
        url = as_text(page.get("fullurl")) or self._article_url(title)
        built, _ = result(title, url, as_text(page.get("extract")), "Wikipedia")
        return built

    def _article_url(self, title: str) -> str:
        if not title:
            return ""
        return self._article_base + quote(title.replace(" ", "_"), safe="")
