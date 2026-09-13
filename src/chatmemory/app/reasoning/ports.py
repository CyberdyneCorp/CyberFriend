"""The collaborators a run needs, as protocols.

Note what the loop is *not* given: a connection string, a SQL tool, a shell,
or a filesystem. Its only route to the corpus is `RetrievalTool`, whose one
method takes a viewer it cannot forge and returns evidence already filtered
by that viewer's access. A crafted message cannot steer the agent towards
rows the permission-filtered surface would not have returned anyway, because
there is no other door -- "the agent must be careful" becomes "the agent
cannot".

`ChatModel` is likewise narrow: two completion shapes, no tool loop. The
federated tool surface is a separate capability with its own authorization.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.verdicts import Assessment
from chatmemory.domain.identity import Viewer
from chatmemory.domain.search import SearchQuery


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Evidence, and what the retrieval surface can say about what it left out.

    `access_blocked` is a count-only signal produced by the permission
    predicate itself: relevant content exists that this viewer may not read.
    It carries no content, is never rendered to a requester, and exists so an
    operator can tell an access problem from an empty corpus.
    """

    items: tuple[Evidence, ...] = ()
    access_blocked: bool = False
    truncated: bool = False
    source_system: str = "discord"


class RetrievalTool(Protocol):
    """The only route to the corpus. Viewer required, always, with no default."""

    async def retrieve(self, viewer: Viewer, query: SearchQuery) -> RetrievalResult:
        """Return evidence from content `viewer` may read.

        Raises `RetrievalUnavailable` when the corpus cannot be consulted --
        never an empty result, which would report an absence of activity that
        was never established.
        """
        ...


class Critic(Protocol):
    """Judges evidence. Returns an enumerated verdict and a score, never prose."""

    async def assess(
        self, question: str, query: SearchQuery, evidence: Sequence[Evidence]
    ) -> Assessment: ...


@dataclass(frozen=True, slots=True)
class Plan:
    sub_questions: tuple[str, ...] = ()
    model_calls: int = 0
    prompt_tokens: int = 0


class Planner(Protocol):
    """Splits a question into separable lines of enquiry."""

    async def plan(self, question: str, max_steps: int) -> Plan: ...


@dataclass(frozen=True, slots=True)
class Grounded:
    """Synthesised text, plus the windows it claims to rest on.

    The driver resolves these ids against evidence the run actually holds;
    ids it does not hold are dropped rather than cited.
    """

    text: str
    cited_window_ids: tuple[int, ...] = ()
    model_calls: int = 1
    prompt_tokens: int = 0


class Synthesizer(Protocol):
    async def synthesize(self, question: str, evidence: Sequence[Evidence]) -> Grounded: ...


class Reranker(Protocol):
    """A cross-encoder over fused candidates.

    `calibrated_for_answerability` is what the relevance gate checks before it
    dares threshold anything: a lexical-overlap scorer also emits values in
    [0, 1], but it measures term rarity, not answerability.
    """

    @property
    def calibrated_for_answerability(self) -> bool: ...

    async def rerank(
        self, question: str, evidence: Sequence[Evidence]
    ) -> Sequence[Evidence]: ...


@dataclass(frozen=True, slots=True)
class JsonCompletion:
    data: Mapping[str, object]
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TextCompletion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatModel(Protocol):
    """An OpenAI-compatible chat handle, narrowed to what the stages need."""

    async def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], schema_name: str
    ) -> JsonCompletion: ...

    async def complete_text(self, system: str, user: str) -> TextCompletion: ...
