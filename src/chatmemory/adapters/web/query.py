"""Where an outbound web query is allowed to come from.

This is the egress boundary the original design deliberately did not have.
The corpus holds private channel content and the agent reads text anyone in
the server can write, so the dangerous shape is not "the agent says something
wrong" but "the agent *asks the internet* something". A crafted message that
steers the agent into searching for a salary band leaks the salary band in
the query string, before any result comes back and before any answer is
rendered.

The federation layer already refuses actions whose `ActionOrigin` is
retrieved content. That covers *whether* a call happens. It does not cover
*what words it carries*: the origin is a label the call site applies, while
the query text is written by a model that has just read untrusted content.

So the words are checked here, against the only text in the run that the
asker actually wrote. A web tool's arguments carry the question as well as
the query, and a query whose significant terms the asker never used is
refused. `asked` is consumed at this boundary and never leaves the process.

The rule is deliberately strict -- subset, not similarity. A model that wants
to search for something the asker did not mention is exactly the case this
exists to stop, and the cost of being wrong in the other direction is a
degraded answer.

This is the *last* check, not the only one: `app.egress` applies the same
rooting rule earlier, where the reasoning loop decides to search at all, and
with a stricter word set (no stopword or plural tolerance). A backstop at the
socket may be looser than the gate upstream -- it then refuses a subset of
what the gate refuses, and never surprises a caller by rejecting something
already cleared -- but it may never be absent, because the thing it guards is
the moment the bytes actually leave the process.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

import structlog

from chatmemory.adapters.mcp_client.routing import STOPWORDS

log = structlog.get_logger()

ARG_QUERY = "query"
"""The only argument whose value is ever sent to a provider."""

ARG_ASKED = "asked"
"""The asking person's own question. Checked against, never transmitted."""

MAX_QUERY_CHARS = 256
"""A query longer than a question is a payload, not a search."""

# Unicode-aware on purpose. An ASCII-only class makes every word written in
# Cyrillic, Greek, Arabic, Hebrew or a CJK script invisible to the containment
# test below, so a query in any of them has no "foreign" terms to find and
# passes trivially. `[^\W_]` is letters and digits in any script.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# Discord renders mentions as `<@123>`, `<#456>`, `<@&789>`. A query carrying
# one is carrying an identifier, whoever typed it.
_DISCORD_MENTION = re.compile(r"<[@#][!&]?\d+>")
_SNOWFLAKE = re.compile(r"\b\d{17,20}\b")


class QueryRefusal(StrEnum):
    """Why a query was not sent. Recorded and logged; never sent as a hint."""

    MISSING_QUESTION = "missing_question"
    EMPTY_QUERY = "empty_query"
    QUERY_TOO_LONG = "query_too_long"
    IDENTIFIER_IN_QUERY = "identifier_in_query"
    NOT_FROM_QUESTION = "not_from_question"


@dataclass(frozen=True, slots=True)
class QueryCheck:
    """The verdict on one proposed query.

    `foreign` names the terms that failed. It is logged for an operator and
    never returned to the model: those words may have come from a crafted
    message, and echoing them back into context is the smuggling route this
    module exists to close.
    """

    query: str = ""
    refusal: QueryRefusal | None = None
    foreign: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.refusal is None


def _stem(word: str) -> str:
    """Fold a trailing plural, so "releases" matches "release".

    Anything cleverer would need a stemmer, and a stemmer is a dependency
    whose behaviour an attacker could probe for a term that slips through.
    """
    return word[:-1] if len(word) >= 4 and word.endswith("s") else word


def terms(text: str) -> frozenset[str]:
    """The significant words of a piece of text, stemmed and lowercased."""
    return frozenset(_stem(w) for w in _WORD.findall(text.lower()) if w not in STOPWORDS)


def check_query(asked: object, query: object) -> QueryCheck:
    """Decide whether this query may be sent, given what the person asked.

    Both arguments arrive as `object` because they come out of a tool-call
    mapping the model wrote: a query that is not even a string is a refusal,
    not a crash.
    """
    if not isinstance(asked, str) or not asked.strip():
        # Without the asker's own words there is nothing to check against,
        # and "nothing to check against" must not mean "anything goes".
        return QueryCheck(refusal=QueryRefusal.MISSING_QUESTION)
    if not isinstance(query, str) or not query.strip():
        return QueryCheck(refusal=QueryRefusal.EMPTY_QUERY)

    cleaned = " ".join(query.split())
    if len(cleaned) > MAX_QUERY_CHARS:
        return QueryCheck(query=cleaned, refusal=QueryRefusal.QUERY_TOO_LONG)
    if _DISCORD_MENTION.search(cleaned) or _SNOWFLAKE.search(cleaned):
        return QueryCheck(query=cleaned, refusal=QueryRefusal.IDENTIFIER_IN_QUERY)

    foreign = tuple(sorted(terms(cleaned) - terms(asked)))
    if foreign:
        return QueryCheck(
            query=cleaned, refusal=QueryRefusal.NOT_FROM_QUESTION, foreign=foreign
        )
    return QueryCheck(query=cleaned)


def web_arguments(question: str, query: str) -> Mapping[str, object]:
    """Build the arguments for a web tool call.

    Offered so a call site cannot forget the question half: a web tool
    invoked without it is refused at the boundary, which turns "remember to
    pass the question" into a failure that shows up the first time rather
    than a check that silently never runs.
    """
    return {ARG_QUERY: query, ARG_ASKED: question}
