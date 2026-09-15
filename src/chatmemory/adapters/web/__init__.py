"""The egress boundary: CyberFriend asking the open internet a question.

The original design left this out on purpose. The agent holds private channel
content and ingests text anyone in the server can write, and an outbound
channel completes that pattern: a crafted message does not have to make the
agent *say* anything to leak: making it *search* for something private puts
the secret in the query string, before a single result comes back.

So a web query may only be built from the asking person's own words, and that
is checked at the boundary rather than trusted upstream (`query`). Everything
else here is the ordinary discipline an outbound dependency needs -- a
timeout, a rate limit, a cap on calls per question, a bounded and attributed
rendering, and failure that degrades one answer instead of ending a run.

These are not MCP servers, but they are governed like them. Each provider
satisfies `ToolSession`, so the allowlist, the router, the invoke-time
authorization and the audit trail apply unchanged; there is no second path
for "simple" tools. `registration` is where that wiring is assembled.

Read in dependency order:

    query          which words may be sent, and which may not
    limits         how often, and how many times per question
    results        what a result is, and how it stays visibly not-the-corpus
    provider       one web API as a governed, degrading tool session
    wikipedia      public, credential-free, always available
    serpapi        Google, present only when its key is
    registration   servers + allowlist + factory, for `connect()`
"""

from __future__ import annotations

from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.provider import WebProvider, WebToolSpec
from chatmemory.adapters.web.query import (
    ARG_ASKED,
    ARG_QUERY,
    QueryCheck,
    QueryRefusal,
    check_query,
    web_arguments,
)
from chatmemory.adapters.web.registration import (
    WebTools,
    WebToolsConfig,
    build_web_tools,
)
from chatmemory.adapters.web.results import (
    SERPAPI_SERVER,
    WIKIPEDIA_SERVER,
    WebResult,
    provider_label,
    render,
    source_system_for,
)
from chatmemory.adapters.web.serpapi import SerpApiProvider
from chatmemory.adapters.web.wikipedia import WikipediaProvider

__all__ = [
    "ARG_ASKED",
    "ARG_QUERY",
    "SERPAPI_SERVER",
    "WIKIPEDIA_SERVER",
    "CallBudget",
    "QueryCheck",
    "QueryRefusal",
    "RateLimiter",
    "SerpApiProvider",
    "WebProvider",
    "WebResult",
    "WebToolSpec",
    "WebTools",
    "WebToolsConfig",
    "WikipediaProvider",
    "build_web_tools",
    "check_query",
    "provider_label",
    "render",
    "source_system_for",
    "web_arguments",
]
