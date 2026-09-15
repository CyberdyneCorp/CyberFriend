"""What a web result is, and how it stays visibly different from the corpus.

A reader must always be able to tell "my colleagues said" from "the internet
says". Three things keep that true from the provider to the answer:

*   Every result carries the provider that produced it and a URL the reader
    can open, in the rendered text itself. Attribution is not reconstructed
    downstream from a tool name.
*   The rendered block opens by naming what it is, so the distinction
    survives being quoted into a prompt as fenced data.
*   `source_system_for` hands the answering layer the same label that
    `Evidence.source_system` carries for Discord windows -- `SOURCE_WEB`,
    imported rather than re-spelled, so a citation from the internet cannot
    be rendered as a citation from a channel and cannot drift apart from the
    one place that decides what counts as the corpus.

Size is bounded here as well as in the federation client. The client's cap
protects the prompt; this one protects it *legibly*, by dropping whole
results and saying so rather than cutting a URL in half.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from chatmemory.app.reasoning.evidence import SOURCE_WEB

WIKIPEDIA_SERVER = "wikipedia"
SERPAPI_SERVER = "serpapi"

WEB_SERVERS = frozenset({WIKIPEDIA_SERVER, SERPAPI_SERVER})

PROVIDER_LABELS: Mapping[str, str] = {
    WIKIPEDIA_SERVER: "Wikipedia",
    # The vendor is named because a reader deserves to know which service
    # answered, while the *source system* below stays vendor-neutral:
    # swapping SerpApi for another search API must not change how an answer
    # distinguishes the internet from the team's own record.
    SERPAPI_SERVER: "Google (via SerpApi)",
}

MAX_SNIPPET_CHARS = 600
CLIP_MARKER = " [...]"
TRUNCATION_NOTE = "[... truncated: web results exceeded the size limit]"
NO_RESULTS_NOTE = "No web results were returned for this query."

_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class WebResult:
    """One thing the internet returned, attributed and openable."""

    title: str
    url: str
    snippet: str
    source: str

    def block(self, index: int) -> str:
        lines = [f"{index}. {self.title}".strip(), f"   source: {self.source}"]
        if self.url:
            lines.append(f"   url: {self.url}")
        if self.snippet:
            lines.append(f"   {self.snippet}")
        return "\n".join(lines)


def clean(text: str) -> str:
    """Strip the markup a search API embeds in its own snippets.

    Providers wrap matched terms in `<span>`s. Left in place they become tags
    inside a fenced block, which is noise at best and a second thing for a
    reader to interpret at worst.
    """
    return " ".join(html.unescape(_TAG.sub(" ", text)).split())


def clip(text: str, limit: int = MAX_SNIPPET_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + CLIP_MARKER, True


def result(title: str, url: str, snippet: str, source: str) -> tuple[WebResult, bool]:
    """Build one result from raw provider text, reporting any clipping."""
    body, clipped = clip(clean(snippet))
    return WebResult(title=clean(title), url=url.strip(), snippet=body, source=source), clipped


def render(
    label: str, query: str, results: Sequence[WebResult], max_chars: int
) -> tuple[str, bool]:
    """Render results as data, bounded, with any loss stated in the text."""
    header = (
        f"Web results from {label}. This is external internet content, "
        "not from this server's conversations.\n"
        f"query: {query}"
    )
    if not results:
        return f"{header}\n\n{NO_RESULTS_NOTE}", False

    blocks: list[str] = []
    used = len(header)
    truncated = False
    for index, item in enumerate(results, start=1):
        block = item.block(index)
        if used + len(block) + 2 > max_chars:
            truncated = True
            break
        blocks.append(block)
        used += len(block) + 2

    body = "\n\n".join([header, *blocks])
    if truncated:
        body = f"{body}\n{TRUNCATION_NOTE}"
    return body, truncated


def source_system_for(qualified_name: str) -> str | None:
    """The evidence source system for a web tool, or None for anything else.

    Takes the qualified `server:tool` name the audit and the evidence context
    already use, so the answering layer labels a result from the same string
    it cites it by. Every provider here maps to the one `SOURCE_WEB` value:
    the distinction a reader needs is corpus versus internet, and a per-vendor
    source system would put that decision in a lookup table that grows.
    """
    server, _, _ = qualified_name.partition(":")
    return SOURCE_WEB if server in WEB_SERVERS else None


def provider_label(server: str) -> str:
    """How an answer names the service that produced a result."""
    return PROVIDER_LABELS.get(server, server)


# --- reading a provider's JSON ------------------------------------------
#
# Every provider payload is untrusted and shaped by someone else's API
# version. These narrow `object` down without ever raising: a payload that
# does not look the way it used to yields an empty result, which degrades the
# answer, rather than an exception, which would mark the provider dead.


def as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return {}


def as_sequence(value: object) -> Sequence[object]:
    if isinstance(value, list):
        return cast("list[object]", value)
    return ()


def as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def as_number(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
