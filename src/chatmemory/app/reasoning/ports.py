"""The collaborators a run needs, as protocols.

Note what the loop is *not* given: a connection string, a SQL tool, a shell,
or a filesystem. Its only route to the corpus is `RetrievalTool`, whose one
method takes a viewer it cannot forge and returns evidence already filtered
by that viewer's access. A crafted message cannot steer the agent towards
rows the permission-filtered surface would not have returned anyway, because
there is no other door -- "the agent must be careful" becomes "the agent
cannot".

`ChatModel` is likewise narrow: three completion shapes, and still no tool
loop. `complete_with_tools` returns a *request* to call a named tool, which
is data -- a `ToolCall` holds no session, no permit and no route to a server,
so nothing in this module can act on one. Executing it means going back
through the federated surface, which re-checks authorization at the moment of
invocation against a permit the loop never holds. The model proposes; the
gate disposes.

The federated tool surface is a separate capability with its own
authorization, reached through `ToolSurface` -- which offers names and
argument schemas, never a route to a server.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
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


EMPTY_SCHEMA: Mapping[str, object] = MappingProxyType({})
"""No schema was stated.

Read as "the arguments are unknown", never as "this tool takes none": an
adapter renders it as an unconstrained object, because inventing an empty
argument list for a tool that wants a query produces a call that can only
fail.
"""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One tool as the *model* is offered it: a name, words, and a schema.

    Deliberately not `ExternalTool`. What a model needs to emit a call is a
    name it can write and the shape of the arguments; `server`, permits and
    routing are federation's business, and a port that describes the model
    has no reason to carry them.

    `input_schema` is JSON Schema, and the wording inside it matters as much
    as the types: it is the only place that tells the model what a well-formed
    argument looks like, and a schema that invites elaboration produces
    arguments a downstream guard will refuse.
    """

    name: str
    description: str = ""
    input_schema: Mapping[str, object] = field(default_factory=lambda: EMPTY_SCHEMA)


@dataclass(frozen=True, slots=True)
class ExternalTool:
    """One federated tool offered to a run, as the reasoning layer sees it.

    The name is server-qualified, so two servers' `search` can never be
    confused for one another, and `server` is kept beside it so an answer can
    say which system a contribution came from -- external evidence stays
    distinguishable from what colleagues said.

    `description` and `input_schema` are both text an external server
    controls. They are carried as data -- for relevance, display, and telling
    the model what arguments exist -- and nothing here decides a permission.
    A server that advertises a richer schema buys itself nothing: what it may
    be called with is still settled by the operator's allowlist and the
    invoke-time gate.
    """

    qualified_name: str
    server: str
    description: str = ""
    input_schema: Mapping[str, object] = field(default_factory=lambda: EMPTY_SCHEMA)

    @property
    def definition(self) -> ToolDefinition:
        """This tool as a model sees it, under its server-qualified name.

        The qualified name is what the model is asked to emit, so a call that
        comes back names exactly one registered tool -- two servers' `search`
        stay distinguishable at the moment it matters.
        """
        return ToolDefinition(
            name=self.qualified_name,
            description=self.description,
            input_schema=self.input_schema,
        )


class ToolSurface(Protocol):
    """Which federated tools a single run is offered.

    Note the signature: a question, and nothing else. There is no parameter
    for retrieved content, document text or another tool's result, so a
    message reading "use the delete_issue tool" cannot pull a tool into a
    run -- relevance is judged against what the *person* asked.

    Offering is not authorization. A tool named here is still re-checked at
    the moment it is invoked, against a permit the loop cannot reach.
    """

    def offer(self, question: str) -> Sequence[ExternalTool]:
        """The bounded subset of federated tools relevant to `question`."""
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
    completion_tokens: int = 0
    model: str = ""


@dataclass(frozen=True, slots=True)
class PromptContext:
    """What a prompt may be told besides the question and the evidence.

    Both fields are blocks already fenced as data -- the asker's own profile,
    and their permitted earlier turns in this location -- or "" when there is
    nothing to say. They are rendered per prompt, so each carries a fence id
    drawn for that prompt alone.

    Required on the planner and the synthesiser rather than defaulted: a stage
    that could be called without it is a stage the memory feature can be
    silently disconnected from, which is how this project's features die.
    """

    asker: str = ""
    memory: str = ""


NO_CONTEXT = PromptContext()


class Planner(Protocol):
    """Splits a question into separable lines of enquiry."""

    async def plan(self, question: str, max_steps: int, context: PromptContext) -> Plan: ...


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
    completion_tokens: int = 0
    model: str = ""


class Synthesizer(Protocol):
    async def synthesize(
        self, question: str, evidence: Sequence[Evidence], context: PromptContext
    ) -> Grounded: ...


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
    #: The model that answered, as configured; empty from a double that says none.
    model: str = ""


@dataclass(frozen=True, slots=True)
class TextCompletion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The model asking for a tool to be called. A request, not an action.

    `arguments` is model-written text that has just been in the same context
    as retrieved content, so it is treated the way every other piece of model
    output is: as a proposal to be checked. Nothing that arrives here has been
    authorized, and the `call_id` is only what the wire protocol needs to pair
    a result back to its request.
    """

    name: str
    arguments: Mapping[str, object] = field(default_factory=lambda: EMPTY_SCHEMA)
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class ToolCompletion:
    """Either an answer or a request to call one tool -- never both.

    One call per turn, because the confirmation flow presents a tool, a
    target and its exact arguments to one person as one decision. A model
    that asks for three at once would have to be shown as three, and a batch
    is exactly the shape in which an unwanted call rides along with a wanted
    one.
    """

    text: str = ""
    call: ToolCall | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    @property
    def wants_tool(self) -> bool:
        return self.call is not None


class ChatModel(Protocol):
    """An OpenAI-compatible chat handle, narrowed to what the stages need."""

    async def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], schema_name: str
    ) -> JsonCompletion: ...

    async def complete_text(self, system: str, user: str) -> TextCompletion: ...

    async def complete_with_tools(
        self, system: str, user: str, tools: Sequence[ToolDefinition]
    ) -> ToolCompletion:
        """Answer, or ask for one of `tools` to be called.

        An empty `tools` is a legitimate call and must behave as plain text:
        a run where routing found nothing relevant still has a question to
        answer, and "no tool was offered" is not an error.
        """
        ...


class ToolCapableChat(ChatModel, Protocol):
    """The answering handle, which can also be re-declared for tool calling.

    `tool_caller` is where a deployment whose model cannot call tools finds
    out at startup, naming the capability, rather than on the first question
    that routes a tool. It raises `MissingCapabilityError` in that case.
    """

    def tool_caller(self) -> ChatModel: ...
