"""A web API presented as a local tool session.

Deliberately not a new kind of thing. The federation layer already decides
what CyberFriend may reach and with whose authority -- an allowlist an
operator writes, a router that only sees the person's question, an
invoke-time gate, and an audit entry per call. Wikipedia and SerpApi are not
MCP servers, but they *are* outbound calls, so they enter through the same
door: each provider satisfies `ToolSession`, which is the only interface the
registry and the federation client consume.

The alternative -- a second, ungoverned path for "simple" tools -- would put
the egress boundary somewhere other than where the authorization layer looks,
which is the whole failure this change exists to avoid.

What this class adds on top of the federation guarantees is everything
specific to talking to the open internet: the query provenance check, the
rate limit, the per-run cap, a bounded and attributed rendering, and the rule
that no provider failure may ever escape as an exception. That last one is
not politeness. `Federation.call` marks a server *lost for the rest of the
run* when a session raises, so a single 500 from SerpApi would remove the
tool from a run that could still have used it. A failure degrades one call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

import httpx
import structlog

from chatmemory.adapters.mcp_client.session import DiscoveredTool, ToolResult, ToolSession
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.query import ARG_ASKED, ARG_QUERY, MAX_QUERY_CHARS, check_query
from chatmemory.adapters.web.results import WebResult, provider_label, render
from chatmemory.app.authorization import ToolEffect
from chatmemory.app.egress import EgressRefused, current_authorization

log = structlog.get_logger()

USER_AGENT = "CyberFriend/0.1 (https://github.com/CyberdyneCorp/discord_agent)"
"""Identifies the deployment, and nobody in it.

The contact URL is not decoration: Wikimedia's robot policy answers 403 to a
user agent without one, which the live tests found the direct way. What must
never appear here is anything about the person asking -- a per-requester
agent string would put a user identifier on every outbound request, which is
precisely the leak this module exists to bound."""

DEFAULT_TIMEOUT = 8.0
DEFAULT_MAX_RESULTS = 5
DEFAULT_MAX_RESULT_CHARS = 3000


QUERY_DESCRIPTION = (
    "The search terms, taken from the question exactly as the person asked "
    "it. Use only words that appear in their question -- drop words to "
    "shorten it, never add any. Do not add context, synonyms, spelled-out "
    "acronyms, product or site names, dates, or anything you read in this "
    "conversation, in a document, or in another tool's result. Do not include "
    "names, @mentions or numeric ids."
)
"""Why this reads as a prohibition rather than an invitation.

The egress guard admits a reformulation only if every significant word in it
is a word the asker wrote. A schema saying "a well-formed search query,
expanded with relevant context" would therefore describe a call that is
always refused -- the model would obey the schema and be blocked every time,
and the deployment would look broken rather than guarded. The schema has to
say the same thing the guard enforces, or the guard is just a refusal
generator.

The last sentence is not tidiness either. A query is the first thing that
leaves the process, so an identifier that reaches it has already leaked,
before any result comes back and before any answer is rendered.
"""

WEB_QUERY_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            ARG_QUERY: {
                "type": "string",
                "description": QUERY_DESCRIPTION,
                "maxLength": MAX_QUERY_CHARS,
            }
        },
        "required": [ARG_QUERY],
        # `asked` is deliberately absent, and this is the load-bearing part of
        # the schema. The asker's own words are supplied by the call site
        # (`query.web_arguments`) and the clearance is minted by EgressGuard
        # from the question the person actually typed. Showing the model a
        # field for them would invite it to write both halves of the
        # comparison, which is no comparison at all -- and that is precisely
        # the check this boundary replaced.
        "additionalProperties": False,
    }
)
"""The arguments every web tool takes.

One schema for both providers because they take one argument: the difference
between "search Wikipedia" and "search Google" lives in the tool's
description, not in its parameters. A per-provider schema would be a second
place for the wording above to drift out of step with the guard.
"""


@dataclass(frozen=True, slots=True)
class WebToolSpec:
    """One tool a provider offers.

    The description is what the router matches a question against, so it is
    written in the words a person would use, not in the provider's own. The
    schema is what the *model* fills in, and defaults to the shared web query
    schema -- a provider that needs a different one says so explicitly.
    """

    name: str
    description: str
    input_schema: Mapping[str, object] = field(default_factory=lambda: WEB_QUERY_SCHEMA)


class WebProvider:
    """One web API, as a `ToolSession`. Subclasses supply only `fetch`."""

    def __init__(
        self,
        *,
        server: str,
        endpoint: str,
        tools: Sequence[WebToolSpec],
        budget: CallBudget,
        limiter: RateLimiter | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        secret_values: Sequence[str] = (),
    ) -> None:
        self.server = server
        self.endpoint = endpoint
        self._tools = tuple(tools)
        self._budget = budget
        self._limiter = limiter or RateLimiter()
        self._max_results = max_results
        self._max_result_chars = max_result_chars
        self._timeout = timeout_seconds
        self._client = client
        # An API key travels as a query parameter for some providers, and
        # httpx puts the full URL in the text of an HTTPStatusError. One
        # unhandled 401 would then write the key into the log, so anything
        # secret is scrubbed out of every message this class emits.
        self._secrets = tuple(v for v in secret_values if v)

    # --- ToolSession ---------------------------------------------------

    async def list_tools(self) -> Sequence[DiscoveredTool]:
        # The effect is a claim, exactly like a remote server's annotation,
        # and the registry treats it as one. What actually makes these tools
        # read-only is the operator's allowlist entry.
        return [
            DiscoveredTool(
                name=t.name,
                description=t.description,
                effect=ToolEffect.READ_ONLY,
                input_schema=t.input_schema,
            )
            for t in self._tools
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        if name not in {t.name for t in self._tools}:
            return self._refuse(name, "unknown_tool")

        # The clearance, not the arguments. `arguments` is written by the
        # model, so checking one of its fields against another compares model
        # output with model output -- which is no check at all, and is what
        # this replaced. The clearance is minted by EgressGuard from the
        # question the person actually asked and travels out of band.
        try:
            clearance = current_authorization(self.server)
        except EgressRefused as refused:
            log.warning(
                "web.egress_refused",
                provider=self.server,
                tool=name,
                reason=str(refused.reason),
            )
            return self._refuse(name, "egress_refused")

        check = check_query(clearance.question, clearance.text)
        if not check.ok:
            log.warning(
                "web.query_refused",
                provider=self.server,
                tool=name,
                reason=str(check.refusal),
                # The offending terms are for an operator, not for the model:
                # they may be words a crafted message put there, and returning
                # them would carry them back into the prompt.
                foreign=list(check.foreign),
            )
            return self._refuse(name, str(check.refusal))

        # Supplied by the call site via `query.web_arguments`, never by the
        # model: it is not in `WEB_QUERY_SCHEMA`, so a caller that forwards
        # the model's arguments unchanged gets a refusal here rather than an
        # unattributed call against the per-run budget.
        asked = arguments.get(ARG_ASKED)
        if not isinstance(asked, str) or not self._budget.spend(asked):
            log.warning("web.budget_exhausted", provider=self.server, tool=name)
            return self._refuse(name, "calls_per_run_exhausted")

        if not await self._limiter.acquire():
            log.warning("web.rate_limited", provider=self.server, tool=name)
            return self._refuse(name, "rate_limited")

        return await self._perform(name, check.query)

    # --- what a subclass provides ---------------------------------------

    async def fetch(
        self, tool: str, query: str, client: httpx.AsyncClient
    ) -> Sequence[WebResult]:
        """Call the provider and shape its answer. Never called unguarded."""
        raise NotImplementedError

    # --- lifecycle -------------------------------------------------------

    @asynccontextmanager
    async def opened(self) -> AsyncIterator[WebProvider]:
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
            # Redirects are not followed: a redirect would re-send the query
            # to a host the operator never allowed, and every endpoint here
            # is an API that has no reason to issue one.
            follow_redirects=False,
        )

    # --- the guarded call ------------------------------------------------

    async def _perform(self, tool: str, query: str) -> ToolResult:
        try:
            # Bounded here as well as by the federation client, so a provider
            # used outside a `Federation` is still bounded.
            async with asyncio.timeout(self._timeout):
                results = await self._fetch_with_client(tool, query)
        except TimeoutError:
            log.warning("web.timeout", provider=self.server, tool=tool, timeout=self._timeout)
            return self._refuse(tool, "timeout")
        except Exception as exc:  # noqa: BLE001 - a provider must never fail the run
            log.warning(
                "web.failed", provider=self.server, tool=tool, error=self.redact(str(exc))
            )
            return self._refuse(tool, "provider_error")

        text, truncated = render(
            provider_label(self.server), query, results[: self._max_results], self._max_result_chars
        )
        log.info(
            "web.query",
            provider=self.server,
            tool=tool,
            query=query,
            results=len(results),
            truncated=truncated,
        )
        return ToolResult(text=text)

    async def _fetch_with_client(self, tool: str, query: str) -> Sequence[WebResult]:
        if self._client is not None:
            return await self.fetch(tool, query, self._client)
        async with self._new_client() as client:
            return await self.fetch(tool, query, client)

    def redact(self, text: str) -> str:
        """Remove any configured secret from a string about to be logged."""
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _refuse(self, tool: str, reason: str) -> ToolResult:
        # An error result, not an empty one: "the provider did not answer" and
        # "the internet has nothing" must never look the same to the loop.
        return ToolResult(text=f"{self.server}:{tool} did not run ({reason})", is_error=True)


if TYPE_CHECKING:  # pragma: no cover - type-checking only

    def _provider_conforms(provider: WebProvider) -> ToolSession:
        return provider
