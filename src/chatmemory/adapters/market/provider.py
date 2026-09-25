"""A market data source presented as a local tool session.

The same door as the web providers, for the same reason: a price lookup is an
outbound call, so it is registered, routed, authorized at invoke time and
audited by the federation layer, and reaches no socket without a clearance
the egress guard minted from the asker's question.

What differs is the clearance rule. A web query must be made of the asker's
words; a market lookup must be made of members of a fixed set, because "ETH"
has to leave when someone asked about ether. That rule lives in
`app.egress.CLOSED_VOCABULARIES`, and a provider here refuses to exist under a
server name that table does not cover -- otherwise it would quietly fall back
to rooting and refuse every real call, or, worse, be mistaken for a provider
that is being held to membership when it is not.

Order of checks in `call_tool`, all before any request: the tool exists; the
clearance exists and is for this provider; the arguments are exactly members
of their vocabularies; what was cleared is exactly what will be sent. Then a
fresh cache entry answers without a request, or the budget, the rate limit,
and a bounded fetch. No failure escapes as an exception, because
`Federation.call` marks a session lost for the rest of the run when one does.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import structlog

from chatmemory.adapters.market.arguments import ArgumentCheck, Lookup
from chatmemory.adapters.market.cache import FreshCache
from chatmemory.adapters.market.quotes import Quote, figure, render
from chatmemory.adapters.mcp_client.session import DiscoveredTool, ToolResult, ToolSession
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.provider import USER_AGENT
from chatmemory.adapters.web.results import as_mapping
from chatmemory.app.authorization import ToolEffect
from chatmemory.app.egress import (
    AuthorizedQuery,
    EgressRefused,
    closed_vocabulary_for,
    current_authorization,
)

log = structlog.get_logger()

DEFAULT_TIMEOUT = 8.0


class ProviderUnavailable(Exception):
    """The source did not produce a figure. Its message is safe to log."""


class UnsupportedBySource(Exception):
    """A valid vocabulary member this source does not publish."""


@dataclass(frozen=True, slots=True)
class MarketToolSpec:
    """The one tool a market provider offers.

    One tool per provider so that one closed vocabulary covers exactly one
    tool: the guard's term cap is then that tool's arity, not the largest
    arity of anything sharing the provider.
    """

    name: str
    description: str
    input_schema: Mapping[str, object]


class MarketProvider(ABC):
    """One market source, as a `ToolSession`. Subclasses parse and fetch."""

    def __init__(
        self,
        *,
        server: str,
        label: str,
        endpoint: str,
        tool: MarketToolSpec,
        budget: CallBudget,
        cache: FreshCache[tuple[str, ...], Quote],
        limiter: RateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        secret_values: Sequence[str] = (),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if closed_vocabulary_for(server) is None:
            raise ValueError(
                f"market provider {server!r} has no closed vocabulary in app.egress; "
                "it would be cleared by rooting, not membership"
            )
        self.server = server
        self.label = label
        self.endpoint = endpoint
        self.tool = tool
        self._budget = budget
        self._cache = cache
        self._limiter = limiter or RateLimiter()
        self._timeout = timeout_seconds
        self._client = client
        self._transport = transport
        self._now = now
        # SerpApi takes its key as a query parameter, and an HTTP error's text
        # carries the URL. Anything secret is scrubbed from every log line.
        self._secrets = tuple(v for v in secret_values if v)

    # --- ToolSession ---------------------------------------------------

    async def list_tools(self) -> Sequence[DiscoveredTool]:
        # A claim, like any server's. What makes it read-only is the
        # operator's allowlist entry written by `registration`.
        return [
            DiscoveredTool(
                name=self.tool.name,
                description=self.tool.description,
                effect=ToolEffect.READ_ONLY,
                input_schema=self.tool.input_schema,
            )
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        if name != self.tool.name:
            return self._refuse("unknown_tool")
        clearance = self._clearance()
        if clearance is None:
            return self._refuse("egress_refused")
        check = self.check_arguments(arguments)
        if not check.ok or check.lookup is None:
            log.warning(
                "market.arguments_refused",
                provider=self.server,
                tool=name,
                reason=str(check.refusal),
            )
            return self._refuse("invalid_arguments")
        lookup = check.lookup
        if not _cleared_exactly(lookup, clearance):
            # The guard cleared one set of members and the call would send
            # another. Only possible if something between the guard and here
            # changed the arguments, which is itself the thing to refuse.
            log.warning("market.clearance_mismatch", provider=self.server, tool=name)
            return self._refuse("not_cleared")
        return await self._answer(lookup, clearance.question)

    # --- what a subclass provides ---------------------------------------

    @abstractmethod
    def check_arguments(self, arguments: Mapping[str, object]) -> ArgumentCheck:
        """Validate against the closed sets. Pure; runs before any request."""

    @abstractmethod
    async def fetch(self, lookup: Lookup, client: httpx.AsyncClient) -> Quote:
        """Ask the source. Raise `ProviderUnavailable` for anything unusable."""

    def describe(self, quote: Quote, lookup: Lookup) -> str:
        """The figure line. Conversion overrides it to apply the amount."""
        return f"{quote.instrument}: {figure(quote.value)} {quote.unit}"

    async def annotate(self, line: str, quote: Quote, question: str) -> str:
        """The figure line with what this source adds for the asker.

        Nothing by default. The crypto source adds the price in the asker's
        preferred currency, so the answer -- shown verbatim -- carries both
        figures and no model converts anything.
        """
        return line

    # --- lifecycle -------------------------------------------------------

    @asynccontextmanager
    async def opened(self) -> AsyncIterator[MarketProvider]:
        """Hold an HTTP client for as long as this session is registered."""
        if self._client is not None:
            yield self
            return
        async with self._new_client() as client:
            self._client = client
            try:
                yield self
            finally:
                self._client = None

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": USER_AGENT},
            # A redirect would re-send the lookup to a host nobody allowed.
            # Frankfurter's old host answers with one, which is why the
            # endpoint is the new host rather than a followed redirect.
            follow_redirects=False,
            transport=self._transport,
        )

    # --- the guarded call ------------------------------------------------

    def _clearance(self) -> AuthorizedQuery | None:
        try:
            return current_authorization(self.server)
        except EgressRefused as refused:
            log.warning(
                "market.egress_refused", provider=self.server, reason=str(refused.reason)
            )
            return None

    async def _answer(self, lookup: Lookup, question: str) -> ToolResult:
        cached = self._cache.get(lookup.terms)
        if cached is not None:
            # A fresh entry only: the cache cannot return anything older.
            return await self._result(cached, lookup, question)
        # Keyed by the question the guard cleared, not by anything the model
        # wrote, so the per-run cap cannot be dodged by varying an argument.
        if not self._budget.spend(question):
            log.warning("market.budget_exhausted", provider=self.server)
            return self._refuse("calls_per_run_exhausted")
        if not await self._limiter.acquire():
            log.warning("market.rate_limited", provider=self.server)
            return self._refuse("rate_limited")
        return await self._perform(lookup, question)

    async def _perform(self, lookup: Lookup, question: str) -> ToolResult:
        try:
            async with asyncio.timeout(self._timeout):
                quote = await self._fetch_with_client(lookup)
        except UnsupportedBySource:
            log.info("market.unsupported", provider=self.server, terms=list(lookup.terms))
            return self._unavailable(lookup, "does not publish this")
        except TimeoutError:
            log.warning("market.timeout", provider=self.server, timeout=self._timeout)
            return self._unavailable(lookup, "did not answer in time")
        except Exception as exc:  # noqa: BLE001 - a provider must never fail the run
            log.warning("market.failed", provider=self.server, error=self.redact(str(exc)))
            return self._unavailable(lookup, "is unavailable")
        self._cache.put(lookup.terms, quote)
        log.info("market.quote", provider=self.server, terms=list(lookup.terms))
        return await self._result(quote, lookup, question)

    async def _result(self, quote: Quote, lookup: Lookup, question: str) -> ToolResult:
        line = await self.annotate(self.describe(quote, lookup), quote, question)
        return ToolResult(text=render(quote, line))

    async def _fetch_with_client(self, lookup: Lookup) -> Quote:
        if self._client is not None:
            return await self.fetch(lookup, self._client)
        async with self._new_client() as client:
            return await self.fetch(lookup, client)

    def redact(self, text: str) -> str:
        """Remove any configured secret from a string about to be logged."""
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def retrieved_at(self) -> datetime:
        return self._now()

    def remember(self, lookup: Lookup, quote: Quote) -> None:
        """Cache a figure a response carried beyond the one asked for.

        Only ever a figure from the response being handled now, so it is as
        fresh as the one returned and expires on the same schedule.
        """
        self._cache.put(lookup.terms, quote)

    def _unavailable(self, lookup: Lookup, why: str) -> ToolResult:
        # Says there is no figure, and says it will not substitute one. An
        # error result, so the loop cannot mistake it for an answer.
        return ToolResult(
            text=(
                f"{self.server}:{self.tool.name} did not run: {self.label} {why} "
                f"for {'/'.join(lookup.terms)}. No current figure is available, "
                "and no earlier figure is substituted."
            ),
            is_error=True,
        )

    def _refuse(self, reason: str) -> ToolResult:
        return ToolResult(
            text=f"{self.server}:{self.tool.name} did not run ({reason})", is_error=True
        )


def _cleared_exactly(lookup: Lookup, clearance: AuthorizedQuery) -> bool:
    """Whether the members about to be sent are the members the guard cleared.

    Compared as sorted multisets, because the guard saw the argument values
    in whatever order the model wrote the keys.
    """
    cleared = sorted(token.upper() for token in clearance.text.split())
    return cleared == sorted(lookup.terms)


def read_json(response: httpx.Response, source: str) -> Mapping[str, object]:
    """A 200 JSON object, with decimals kept exact, or `ProviderUnavailable`.

    Not `raise_for_status`: its message embeds the request URL, which for
    SerpApi holds the key.
    """
    if response.status_code != 200:
        raise ProviderUnavailable(f"{source} returned HTTP {response.status_code}")
    try:
        payload = json.loads(response.text, parse_float=Decimal)
    except ValueError as exc:
        raise ProviderUnavailable(f"{source} returned something other than JSON") from exc
    return as_mapping(payload)


def as_decimal(value: object) -> Decimal | None:
    """A finite positive number from a payload parsed with `read_json`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = Decimal(value)
    if isinstance(value, Decimal) and value.is_finite() and value > 0:
        return value
    return None


if TYPE_CHECKING:  # pragma: no cover - type-checking only

    def _provider_conforms(provider: MarketProvider) -> ToolSession:
        return provider
