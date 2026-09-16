"""Choosing which federated tools a single run gets to see.

Two reasons to route rather than expose everything, and they are not the same
reason. The weaker one is quality: tool selection degrades as the count
grows, though that is a well-founded prior here rather than a curve we have
measured, and the honest thing is to say so. The stronger one is blast
radius: a tool that was never offered is one the invoke-time check refuses
outright, so routing narrows what a confused or steered loop can even name.

Note the signature of `route`: a question and a registration. There is no
parameter for retrieved content, so a message saying "you should use the
delete_issue tool" cannot pull a tool into a run. Relevance is judged against
what the *person* asked, and only that.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from chatmemory.adapters.mcp_client.registry import RegisteredTool, Registration

log = structlog.get_logger()

_WORD = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "be", "been", "but", "by", "can", "did", "do",
    "does", "for", "from", "get", "got", "had", "has", "have", "how", "i", "if", "in", "into",
    "is", "it", "its", "me", "my", "no", "not", "of", "on", "or", "our", "out", "so", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "to", "too", "us", "was",
    "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "would",
    "you", "your",
})


def _terms(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in STOPWORDS)


@dataclass(frozen=True, slots=True)
class RoutedTools:
    """The bounded subset offered to one run.

    `offers` is consulted at invocation as well as at offer time. The
    reasoning loop cannot add to this set, because it never holds one that is
    not frozen.
    """

    question: str
    tools: tuple[RegisteredTool, ...] = ()

    @property
    def names(self) -> frozenset[str]:
        return frozenset(t.qualified_name for t in self.tools)

    def offers(self, qualified_name: str) -> bool:
        return qualified_name in self.names

    @property
    def is_empty(self) -> bool:
        return not self.tools


@dataclass(frozen=True, slots=True)
class _Scored:
    tool: RegisteredTool
    score: int

    @property
    def key(self) -> tuple[int, str]:
        # Descending score, then name, so routing is reproducible: two runs of
        # the same question must offer the same tools or the invoke-time
        # refusals become flaky rather than informative.
        return (-self.score, self.tool.qualified_name)


class ToolRouter:
    """Lexical overlap between the question and each tool's own words.

    Deliberately not a model call. Routing runs on every federated question,
    a wrong choice costs an unused tool rather than a wrong answer, and a
    deterministic router is one the injection corpus can assert against.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("a run must be able to see at least one tool")
        self._limit = limit

    def route(self, question: str, registration: Registration) -> RoutedTools:
        tools = tuple(registration.tools)

        # Routing exists to bound the prompt, not to be clever. When every
        # registered tool fits inside the per-run limit there is nothing to
        # choose between, and choosing anyway was actively harmful: the score
        # is word overlap against a tool's DESCRIPTION, and a description
        # names a category ("resolves a package name to a library id") while
        # a question names an instance ("fastapi dependency injection").
        # Those share no words, so a real question scored zero against a tool
        # that was exactly right and the run was offered nothing at all.
        # Split by effect, because the two kinds of mistake are not
        # comparable. Offering a read-only tool that turns out to be
        # irrelevant costs a wasted call the model usually does not make;
        # offering a MUTATING one puts a destructive action in front of a
        # possibly-steered loop, with only the confirmation between them.
        # So read-only tools are offered whenever they fit, and a mutating
        # tool must still earn its place by matching the question.
        read_only = tuple(t for t in tools if not t.permit.effect.mutates)
        mutating = tuple(t for t in tools if t.permit.effect.mutates)
        asked_terms = _terms(question)
        earned = tuple(
            t
            for t in mutating
            if asked_terms & _terms(f"{t.permit.tool} {t.description}")
        )
        tools = read_only + earned

        if len(tools) <= self._limit:
            chosen = tools
        else:
            asked = _terms(question)
            scored = [
                _Scored(
                    tool=tool,
                    score=len(asked & _terms(f"{tool.permit.tool} {tool.description}")),
                )
                for tool in tools
            ]
            # Above the limit something has to be dropped. Overlap is a poor
            # ranking for the reason above, so a tool that scores zero is kept
            # as a candidate rather than excluded -- ordering is the job here,
            # not exclusion.
            chosen = tuple(s.tool for s in sorted(scored, key=lambda s: s.key)[: self._limit])
        log.info(
            "federation.routed",
            considered=len(tools),
            offered=len(chosen),
            limit=self._limit,
        )
        return RoutedTools(question=question, tools=chosen)


def describe(tools: Sequence[RegisteredTool]) -> str:
    """A one-line-per-tool rendering for a prompt's tool section."""
    return "\n".join(f"{t.qualified_name}: {t.description}" for t in tools)
