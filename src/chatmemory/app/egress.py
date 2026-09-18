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

There is exactly one exception to rooting, and it is narrower than rooting
rather than looser: a provider listed in `CLOSED_VOCABULARIES`. Its arguments
are instruments or ISO 4217 codes, so someone asking about "ETH" or "reais"
needs "ETH" or "BRL" to leave, and those are words the asker may never have
typed. For those providers alone, rooting is replaced by *membership*: every
token must be a member of the provider's fixed set, the number of tokens is
capped, and anything else is refused outright -- never trimmed. A fixed list
cannot carry free text, which is the thing rooting exists to stop. The table
is a constant here, at the boundary, and not something an adapter registers:
a declaration a caller could make is a declaration a caller could widen.

Every call is audited -- allowed or refused -- with who asked, what they
asked, what was sent and to which provider. A refusal is the more interesting
record of the two: it is what an attempted exfiltration looks like from the
outside.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
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


def keep_asker_words(text: str, question: str) -> str:
    """`text` with every word the asker did not write removed.

    The rooting check refuses a query containing any word the asker did not
    type, which is the point -- it is what stops retrieved content leaving as
    a search term. But a model reformulating a question for a search engine
    routinely adds a helpful word of its own: asked "who wrote the novel
    Dune", it proposed "Dune novel author", and "author" is not the asker's.
    The call was refused and the question went unanswered.

    Dropping those words rather than the whole call keeps the invariant
    exactly -- everything that leaves is a word the asker wrote -- while
    keeping the model's useful selection and ordering. It is not a loosening:
    a word that was not in the question still cannot leave, whoever
    proposed it.
    """
    allowed = _words(question)
    kept = [m.group() for m in _WORD.finditer(text) if m.group().casefold() in allowed]
    return " ".join(kept)


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


@dataclass(frozen=True, slots=True)
class ClosedVocabulary:
    """A fixed set of terms, the only things a provider may be sent.

    `admits` splits on whitespace, not on the word pattern rooting uses, and
    compares whole tokens: `USD;` or `USD-EUR` is not a member, so punctuation
    cannot ride along beside a valid code. Case is folded because a model may
    write `eth`; the provider sends its canonical spelling, so case never
    leaves and cannot carry anything either.

    `max_terms` bounds how many members one call may carry. A sequence of
    codes is still an alphabet, and the cap is what keeps the channel it
    leaves open to a handful of bits per call.
    """

    terms: frozenset[str]
    max_terms: int

    def __post_init__(self) -> None:
        if not self.terms or self.max_terms < 1:
            raise ValueError("a closed vocabulary needs terms and a positive term cap")

    def admits(self, text: str) -> bool:
        tokens = text.split()
        if not tokens or len(tokens) > self.max_terms:
            return False
        members = {term.casefold() for term in self.terms}
        return all(token.casefold() in members for token in tokens)


CRYPTO_ASSETS = frozenset({"BTC", "ETH"})
MARKET_INDICES = frozenset({"SPX"})

_ISO_4217_TABLE = """
    AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BHD BIF BMD BND BOB
    BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP
    DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF
    IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK
    LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN
    NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF
    SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND
    TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VED VES VND VUV WST XAF XCD XCG XOF
    XPF YER ZAR ZMW ZWG
"""

ISO_4217_CODES = frozenset(_ISO_4217_TABLE.split())
"""ISO 4217 currency codes a person could mean by "convert".

The active circulating currencies only. Fund codes (USN, CLF, ...), precious
metals (XAU), bond-market units and the testing codes (XTS, XXX) are ISO 4217
too, but no one converts into them, and every member left out is one fewer
symbol a call can carry. Withdrawn codes are absent too: ANG (replaced by
XCG in 2025) and BGN (Bulgaria joined the euro in 2026). A valid code the rate
source does not publish is reported as unsupported by that source, after the
check, not before it."""

CHAIN_BALANCES_PROVIDER = "chain_balances"
"""The wallet lookup. Deliberately absent from `CLOSED_VOCABULARIES` below.

Held to rooting instead, because addresses are a set of size 2^160 and there
is no membership to check. Rooting is what makes the lookup reach only an
address the asker typed into their own question, so the assistant can never be
used to sweep the addresses mentioned across the channels it can read.
"""

MARKET_CRYPTO_PROVIDER = "market_crypto"
MARKET_FX_PROVIDER = "market_fx"
MARKET_INDEX_PROVIDER = "market_index"

CLOSED_VOCABULARIES: Mapping[str, ClosedVocabulary] = MappingProxyType(
    {
        # One provider per vocabulary rather than one "market" provider with
        # the union: clearance is issued per provider, so a crypto lookup
        # cannot be cleared to carry a currency code, and each cap is the
        # exact arity of the one tool behind it.
        MARKET_CRYPTO_PROVIDER: ClosedVocabulary(CRYPTO_ASSETS, max_terms=1),
        MARKET_INDEX_PROVIDER: ClosedVocabulary(MARKET_INDICES, max_terms=1),
        MARKET_FX_PROVIDER: ClosedVocabulary(ISO_4217_CODES, max_terms=2),
    }
)
"""Providers checked by membership instead of rooting. Nothing else is.

Keyed by provider because the provider is what the guard is told about; every
tool a listed provider offers must take only closed-vocabulary text arguments.
Numbers are not text and are not checked here -- which is why a conversion
amount is applied locally and never sent (see `adapters.market`)."""


def closed_vocabulary_for(provider: str) -> ClosedVocabulary | None:
    """The vocabulary a provider is held to, or None when rooting applies."""
    return CLOSED_VOCABULARIES.get(provider)


class RefusalReason(StrEnum):
    """Why a call did not leave. Recorded; never rendered to a requester."""
    UNAUTHORIZED = "unauthorized"
    """Reached without clearance, so the guard never saw this call."""


    EMPTY_QUERY = "empty_query"
    CONTENT_DERIVED = "content_derived"
    NOT_ROOTED_IN_QUESTION = "not_rooted_in_question"
    OUTSIDE_CLOSED_VOCABULARY = "outside_closed_vocabulary"
    """A closed-vocabulary provider was handed something not in its set.

    Deliberately a different reason from NOT_ROOTED_IN_QUESTION: the invoker
    retries a not-rooted read-only call with the unrooted words trimmed, and a
    closed-vocabulary argument must be refused outright, not trimmed into
    whatever part of it happened to be a member."""


def refusal_for(
    query: ProvenancedQuery, vocabulary: ClosedVocabulary | None = None
) -> RefusalReason | None:
    """The one check. Pure, total, and the only thing that authorises egress.

    `vocabulary` replaces rooting and nothing else: the empty and
    content-derived gates apply to a closed-vocabulary provider exactly as to
    any other.
    """
    if not query.text.strip():
        return RefusalReason.EMPTY_QUERY
    if query.content_derived:
        return RefusalReason.CONTENT_DERIVED
    if vocabulary is not None:
        return None if vocabulary.admits(query.text) else RefusalReason.OUTSIDE_CLOSED_VOCABULARY
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
    closed_vocabulary: bool = False
    """Whether membership, not rooting, decided this call. An operator
    reading a record needs to know which rule let the words out."""


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
            closed_vocabulary=entry.closed_vocabulary,
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
        vocabulary = closed_vocabulary_for(request.provider)
        refusal = refusal_for(request.query, vocabulary)
        self._audit.record(
            EgressRecord(
                provider=request.provider,
                asker=request.asker,
                question=request.query.question,
                query=request.query.text,
                origin=request.query.origin,
                allowed=refusal is None,
                refusal=refusal,
                closed_vocabulary=vocabulary is not None,
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
