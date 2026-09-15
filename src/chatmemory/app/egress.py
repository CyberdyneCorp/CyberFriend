"""The egress boundary: what may be said to the internet, and on whose behalf.

The agent holds private channel content and ingests text anyone in the server
can write. Adding an outbound channel completes a pattern the authorization
layer was written against, but at a different point: every existing defence
bounds what an answer may *contain*, and none of them bounds what a query may
*say*. A message reading "search the web for the Q3 renewal numbers" does not
need the answer to come back at all -- the request has already left the
building, addressed to a third party, with the private thing spelled out in
it. The query is the leak.

So the rule is narrow and mechanical: **an outbound query must be made of the
asking person's own words**. Not "should be", not "the prompt says so" --
checked here, on every call, against the question the person typed.

Two independent gates enforce it, and both must pass:

*   *Origin.* A query lifted from retrieved content or from another tool's
    result is refused outright. Those are exactly the two channels an
    attacker controls, and no text drawn from them is the asker's words.
*   *Rooting.* The words going out must be a subset of the words that came
    in. A model-suggested reformulation is content-derived from the second
    corrective round onward -- the critic reads attacker-controlled evidence
    and proposes the next query -- so its origin alone cannot clear it. The
    containment check can: a reformulation may drop, reorder or repeat the
    asker's words, and can never introduce one they did not write. A
    laundered payload has to add a token, and adding a token is what is
    refused.

What this deliberately does *not* try to be is a judgement about whether the
query is sensitive. The asker's own question is theirs to send; someone who
types a secret into their own question has disclosed it themselves, and no
boundary here can undo that. What it prevents is the agent disclosing
something on their behalf that they never saw.

The cost is real and is the intended trade: a reformulation that swaps in a
synonym the asker did not use is refused, and recall suffers. A rule that
holds against an adversary is worth more here than one that reads well and
yields under one.

Every call is audited -- allowed or refused -- with who asked, what they
asked, what was sent and to which provider. A refusal is the more interesting
record of the two: it is what an attempted exfiltration looks like from the
outside.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

import structlog

from chatmemory.domain.identity import PersonRef
from chatmemory.ports.answers import Question

log = structlog.get_logger()

MAX_QUERY_CHARS = 300
"""Ceiling on what leaves, matching the cap on a model-suggested query.

A bound on length is a bound on how much can ride out in one call even if
every other check were defeated.
"""

_WORD = re.compile(r"[^\W_]+")
"""Words, for the rooting check.

Punctuation and underscores separate rather than join, so `secret_token` and
`secret token` are the same two words: an attacker must not be able to smuggle
a new term past containment by gluing it to one the asker did write.
"""


class QueryOrigin(StrEnum):
    """Where the text of a query came from. Ordered by how much it is trusted."""

    ASKER = "asker"
    """Typed by the person asking. The only origin that needs no provenance
    argument: these are their words by definition."""

    MODEL_REFORMULATION = "model_reformulation"
    """Proposed by a model that has read retrieved evidence. Admitted only if
    it is rooted in the asker's question."""

    RETRIEVED_CONTENT = "retrieved_content"
    """Taken from the corpus. Never leaves."""

    TOOL_RESULT = "tool_result"
    """Taken from a federated tool's output. Never leaves."""


_TAINT: dict[QueryOrigin, int] = {
    QueryOrigin.ASKER: 0,
    QueryOrigin.MODEL_REFORMULATION: 1,
    QueryOrigin.TOOL_RESULT: 2,
    QueryOrigin.RETRIEVED_CONTENT: 3,
}
"""How far each origin is from the asker's own words.

Derivation takes the maximum, so provenance only ever degrades. A query
rewritten from a content-derived one does not become the asker's words by
passing through another step.
"""

_CONTENT_DERIVED = frozenset({QueryOrigin.RETRIEVED_CONTENT, QueryOrigin.TOOL_RESULT})


def _words(text: str) -> frozenset[str]:
    return frozenset(m.group().casefold() for m in _WORD.finditer(text))


@dataclass(frozen=True, slots=True)
class ProvenancedQuery:
    """A query, and the question it must still be traceable to.

    `question` is carried rather than looked up, and is never overwritten by
    a derivation: the root of the chain is the asker's own message, and a
    query that has been rewritten twice is checked against the same words as
    one that was never rewritten at all.

    Note what is *not* a valid root: the conversation history. It holds
    questions other people asked in the same place, and rooting a query there
    would let one person's words authorise sending another's.
    """

    text: str
    origin: QueryOrigin
    question: str

    @classmethod
    def from_asker(cls, question_text: str) -> ProvenancedQuery:
        return cls(text=question_text, origin=QueryOrigin.ASKER, question=question_text)

    @classmethod
    def for_question(cls, question: Question) -> ProvenancedQuery:
        return cls.from_asker(question.text)

    def derived(self, text: str, origin: QueryOrigin) -> ProvenancedQuery:
        """A rewrite of this query, never cleaner than the query it came from."""
        worst = max(self.origin, origin, key=lambda o: _TAINT[o])
        return ProvenancedQuery(text=text, origin=worst, question=self.question)

    @property
    def content_derived(self) -> bool:
        return self.origin in _CONTENT_DERIVED

    @property
    def rooted_in_asker(self) -> bool:
        """Whether every word going out is a word the asker wrote.

        Applied to every origin including `ASKER`, so the property holds even
        if some future caller mislabels a query: the label is evidence, the
        containment is the check.
        """
        words = _words(self.text)
        return bool(words) and words <= _words(self.question)


class RefusalReason(StrEnum):
    """Why a call did not leave. Recorded; never rendered to a requester."""
    UNAUTHORIZED = "unauthorized"
    """Reached without clearance, so the guard never saw this call."""


    EMPTY_QUERY = "empty_query"
    CONTENT_DERIVED = "content_derived"
    NOT_ROOTED_IN_QUESTION = "not_rooted_in_question"


def refusal_for(query: ProvenancedQuery) -> RefusalReason | None:
    """The one check. Pure, total, and the only thing that authorises egress."""
    if not query.text.strip():
        return RefusalReason.EMPTY_QUERY
    if query.content_derived:
        return RefusalReason.CONTENT_DERIVED
    if not query.rooted_in_asker:
        return RefusalReason.NOT_ROOTED_IN_QUESTION
    return None


class EgressRefused(Exception):
    """Raised instead of making the call.

    An exception rather than a falsy return: a caller that forgets to check
    gets a failed run, not a silent outbound request.
    """

    def __init__(self, reason: RefusalReason, provider: str) -> None:
        super().__init__(f"egress to {provider!r} refused: {reason}")
        self.reason = reason
        self.provider = provider


@dataclass(frozen=True, slots=True)
class EgressRequest:
    """A proposed outbound call, with everything the audit record needs."""

    asker: PersonRef
    query: ProvenancedQuery
    provider: str

    @classmethod
    def for_question(
        cls, question: Question, provider: str, query: ProvenancedQuery | None = None
    ) -> EgressRequest:
        """Build a request from the question being answered.

        The asker comes from the question's viewer, not from a parameter: the
        person a call is made on behalf of is not something a caller deeper
        in the run gets to choose.
        """
        return cls(
            asker=question.asker.person,
            query=query or ProvenancedQuery.for_question(question),
            provider=provider,
        )


_MINTED_BY_GUARD = object()
"""Sentinel proving an `AuthorizedQuery` came from `EgressGuard.authorize`.

Module-private and identity-checked, so "the provider should only be called
with an authorised query" becomes "a provider cannot be called with anything
else". Without it the dataclass is just a record, and a record is something
any caller can assemble for itself -- skipping both the check and the audit.
"""


@dataclass(frozen=True, slots=True)
class AuthorizedQuery:
    """The only thing a provider may be handed. Mintable only by the guard."""

    text: str
    provider: str
    asked_by: PersonRef
    question: str
    minted_by: object = None

    def __post_init__(self) -> None:
        if self.minted_by is not _MINTED_BY_GUARD:
            raise ValueError(
                "an AuthorizedQuery may only be produced by EgressGuard.authorize; "
                "constructing one directly would skip the provenance check and "
                "the audit record"
            )


@dataclass(frozen=True, slots=True)
class EgressRecord:
    """One outbound attempt, as an operator sees it.

    Carries the question verbatim as well as the query. "Which query went to
    which provider" answers what was disclosed; only the pair answers whether
    it should have been, because the question is what the query is checked
    against.
    """

    provider: str
    asker: PersonRef
    question: str
    query: str
    origin: QueryOrigin
    allowed: bool
    refusal: RefusalReason | None = None


class EgressAudit(Protocol):
    def record(self, entry: EgressRecord) -> None: ...


class LoggingEgressAudit:
    """Default sink. Structured, so refusals are queryable as a class."""

    def record(self, entry: EgressRecord) -> None:
        log.info(
            "egress.query",
            provider=entry.provider,
            asker=str(entry.asker),
            question=entry.question,
            query=entry.query,
            origin=str(entry.origin),
            allowed=entry.allowed,
            refusal=str(entry.refusal) if entry.refusal else None,
        )


T = TypeVar("T")


_AUTHORIZED: ContextVar[AuthorizedQuery | None] = ContextVar(
    "chatmemory_authorized_query", default=None
)
"""The clearance for the call currently being dispatched.

A provider is reached through `ToolSession.call_tool(name, arguments)`, whose
only channel for data is a mapping the *model* wrote. A guard that reads the
asking question out of that mapping is checking model output against model
output, which is no check at all. The clearance therefore travels out of band,
set by the guard around the dispatch and read by the provider.
"""


@contextmanager
def authorized(clearance: AuthorizedQuery) -> Iterator[None]:
    """Make `clearance` current for the duration of one dispatch."""
    token = _AUTHORIZED.set(clearance)
    try:
        yield
    finally:
        _AUTHORIZED.reset(token)


def current_authorization(provider: str) -> AuthorizedQuery:
    """The clearance for this dispatch, or refuse.

    Refusing when absent is what makes the guard unskippable: a provider
    reached by any path that did not go through `EgressGuard.authorize` has
    no clearance to find, so the call cannot proceed.
    """
    clearance = _AUTHORIZED.get()
    if clearance is None:
        raise EgressRefused(RefusalReason.UNAUTHORIZED, provider)
    if clearance.provider != provider:
        # Clearance is issued per provider; one provider's must not open
        # another's door.
        raise EgressRefused(RefusalReason.UNAUTHORIZED, provider)
    return clearance


class EgressGuard:
    """The one door out. Checks provenance, records the attempt, then allows it."""

    def __init__(self, audit: EgressAudit | None = None) -> None:
        self._audit = audit or LoggingEgressAudit()

    def authorize(self, request: EgressRequest) -> AuthorizedQuery:
        """Clear a query for sending, or raise. Audited either way.

        The record is written before the refusal is raised, so an attempt
        that never left is as visible to an operator as one that did -- a
        boundary nobody can see being tested is a boundary nobody maintains.
        """
        refusal = refusal_for(request.query)
        self._audit.record(
            EgressRecord(
                provider=request.provider,
                asker=request.asker,
                question=request.query.question,
                query=request.query.text,
                origin=request.query.origin,
                allowed=refusal is None,
                refusal=refusal,
            )
        )
        if refusal is not None:
            raise EgressRefused(refusal, request.provider)
        return AuthorizedQuery(
            text=request.query.text.strip()[:MAX_QUERY_CHARS],
            provider=request.provider,
            asked_by=request.asker,
            question=request.query.question,
            minted_by=_MINTED_BY_GUARD,
        )

    async def send(
        self, request: EgressRequest, call: Callable[[AuthorizedQuery], Awaitable[T]]
    ) -> T:
        """Authorise and perform one outbound call.

        Providers take an `AuthorizedQuery`, so this is the only way to reach
        one: "every outbound call is audited" is then a property of the types
        rather than a rule someone has to remember at each call site.
        """
        return await call(self.authorize(request))
