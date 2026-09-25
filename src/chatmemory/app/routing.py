"""Which path answers a question.

The fixed path carries nearly all the traffic, so the classifier's job is to
find the minority of questions it cannot serve: those with separable
sub-goals, those whose answer is derived state rather than a lookup, and
those reaching an external system.

It is lexical on purpose. A model call here would add latency and
nondeterminism to *every* question, including the ones that route to the
cheap path precisely to avoid paying for a model. That also makes routing a
decision made with zero model calls, which the provenance record asserts
rather than describes.

The classifier is a new failure surface, and its errors are asymmetric:
misrouting a hard question to the fixed path yields a shallow answer;
misrouting an easy one wastes money. The labelled eval set in
`tests/unit/test_routing_eval.py` records where it is currently wrong.

There is a third destination, decided further down by `obligation_question`
rather than by `classify`: obligation questions leave both retrieval paths
entirely and are answered from the `ask` rows. It is kept separate so that
`classify` still answers exactly one question -- fixed or loop -- for every
caller that asks it, including the one that runs after an obligation question
has already been declined. `decision_question` is a fourth, answered from the
`decision` rows the same way.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from enum import StrEnum

from chatmemory.app.language import Language
from chatmemory.app.timespan import Span, cut_span, cut_topic, fold
from chatmemory.domain.chain import find_addresses
from chatmemory.ports.facts import FactKind


class Route(StrEnum):
    FIXED = "fixed"
    LOOP = "loop"


class RoutingSignal(StrEnum):
    """Why a question was routed to the loop. Absent means the fixed path."""

    SEPARABLE_SUBGOALS = "separable_subgoals"
    MULTIPLE_QUESTIONS = "multiple_questions"
    COMPARISON = "comparison"
    DERIVED_STATE = "derived_state"
    EXTERNAL_SYSTEM = "external_system"


# Connectors that join two lines of enquiry rather than two noun phrases.
# "bugs and features" is one lookup; "what did X say and whether Y replied"
# is two, and the difference is the verb phrase after the connector.
_SUBGOAL_CONNECTORS = (
    " and then ", " and also ", " as well as ", " and whether ", " and who ",
    " and what ", " and when ", " and which ", " and how ", " and if ",
    " and did ", " and was ", " and is ", " after that ", "; then ",
)

_COMPARISON_TERMS = (
    "compare", "comparison", " versus ", " vs ", " vs. ", "difference between",
    "how does it differ", "which of",
)

# Questions whose answer is state derived from several lookups: what was
# asked of me, which of those I already answered, and which are still open.
# They read like simple lookups and are not, which is the misroute that costs
# the most -- the motivating case for this whole change.
_DERIVED_STATE_TERMS = (
    "need to do", "do i need", "on my plate", "still open", "still waiting",
    "waiting on me", "waiting on you", "follow up", "followed up", "follow-up",
    "outstanding", "unanswered", "did i reply", "haven't replied", "owe ",
    "action items", "my todos", "to-do", "what should i", "anything i missed",
    "catch me up", "what did i miss",
)

_EXTERNAL_SYSTEM_TERMS = (
    "github", "gitlab", "linear", "jira", "notion", "sentry", "pagerduty",
    "confluence", "pull request", " pr ", "issue tracker", "ticket", "calendar",
)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Where the question goes, and the cheap signals that decided it.

    `model_calls` is zero by construction: nothing here calls a model.
    """

    route: Route
    signals: frozenset[RoutingSignal] = frozenset()
    model_calls: int = 0

    @property
    def reason(self) -> str:
        return ", ".join(sorted(str(s) for s in self.signals)) or "single_lookup"


def _normalise(text: str) -> str:
    return " " + re.sub(r"\s+", " ", text.lower().strip()) + " "


def signals_in(text: str) -> frozenset[RoutingSignal]:
    """Every loop-indicating signal present in the question."""
    padded = _normalise(text)
    found: set[RoutingSignal] = set()
    if any(term in padded for term in _SUBGOAL_CONNECTORS):
        found.add(RoutingSignal.SEPARABLE_SUBGOALS)
    if text.count("?") > 1:
        found.add(RoutingSignal.MULTIPLE_QUESTIONS)
    if any(term in padded for term in _COMPARISON_TERMS):
        found.add(RoutingSignal.COMPARISON)
    if any(term in padded for term in _DERIVED_STATE_TERMS):
        found.add(RoutingSignal.DERIVED_STATE)
    if any(term in padded for term in _EXTERNAL_SYSTEM_TERMS):
        found.add(RoutingSignal.EXTERNAL_SYSTEM)
    return frozenset(found)


def classify(text: str) -> RoutingDecision:
    """Route a question. Any loop signal is enough; none means the fixed path."""
    found = signals_in(text)
    return RoutingDecision(Route.LOOP if found else Route.FIXED, found)


@dataclass(frozen=True, slots=True)
class LabelledQuestion:
    text: str
    expected: Route


@dataclass(frozen=True, slots=True)
class ConfusionMatrix:
    """Counts named from the loop's point of view, since that is the costly call."""

    loop_correct: int = 0
    fixed_correct: int = 0
    routed_to_loop_but_fixed: int = 0
    routed_to_fixed_but_loop: int = 0

    @property
    def total(self) -> int:
        return (
            self.loop_correct
            + self.fixed_correct
            + self.routed_to_loop_but_fixed
            + self.routed_to_fixed_but_loop
        )

    @property
    def accuracy(self) -> float:
        return (self.loop_correct + self.fixed_correct) / self.total if self.total else 0.0


def confusion_matrix(
    examples: Sequence[LabelledQuestion],
    classifier: Callable[[str], RoutingDecision] = classify,
) -> ConfusionMatrix:
    """Score a labelled set. Reported per cell, because the two errors differ:
    a hard question on the fixed path answers shallowly, an easy one on the
    loop merely costs more."""
    counts = {"ll": 0, "ff": 0, "fl": 0, "lf": 0}
    for example in examples:
        actual = classifier(example.text).route
        if example.expected is Route.LOOP:
            counts["ll" if actual is Route.LOOP else "lf"] += 1
        else:
            counts["ff" if actual is Route.FIXED else "fl"] += 1
    return ConfusionMatrix(
        loop_correct=counts["ll"],
        fixed_correct=counts["ff"],
        routed_to_loop_but_fixed=counts["fl"],
        routed_to_fixed_but_loop=counts["lf"],
    )


# --- obligations -------------------------------------------------------
#
# "What did people ask me today" is a *filter over structured rows* -- by
# addressee, by status, by time -- not a search for text that resembles the
# question. Embedding it and hoping the right windows surface is precisely how
# this feature fails, which is why the `ask` rows exist at all. So obligation
# questions are recognised here, before either retrieval path is chosen, and
# answered from records by `app.asks.answering`.
#
# This decision is lexical for the same reason the route above is: it is taken
# on every question, and a model call here would tax the cheap path it exists
# to protect. It is deliberately *narrow*. A question it misses is answered by
# retrieval as before -- a worse answer, not a wrong one -- while a question it
# claims wrongly is answered from the wrong instrument entirely.


class ObligationIntent(StrEnum):
    """Which obligation question was asked.

    The two differ in what belongs in the answer: what other people asked of
    me excludes my own promises, while what I need to do includes them.
    """

    ASKED_OF_ME = "asked_of_me"
    MY_OBLIGATIONS = "my_obligations"


# Phrases that name the viewer as the person asked. Each already contains the
# self-reference, so they need no further guard.
_ASKED_OF_ME_TERMS = (
    "ask me", "asked me", "asking me", "asks me", "ask of me", "asked of me",
    "ask me to", "asked me to", "ask anything of me", "requested of me",
    "asked me for", "want from me", "wants from me", "wanted from me",
)

# Phrases that name an outstanding duty. Every one of them also has to carry a
# first-person reference: "what does sam need to do" is a question about
# somebody else, and answering it from the asker's own obligation rows would
# be both wrong and a small disclosure of how the feature works.
_MY_OBLIGATION_TERMS = (
    "need to do", "needs doing", "on my plate", "todo", "to-do", "to do list",
    "action item", "still open", "outstanding", "waiting on me", "owe",
    "on the hook", "supposed to do", "committed to", "promise", "should i do",
    "should i be doing", "my tasks", "my list", "left to do", "pending on me",
)

_SELF_REFERENCE = re.compile(r"\b(i|me|my|mine|i'm|im|i've|ive)\b", re.IGNORECASE)

# Signals that say the question is not a single lookup. An obligation filter
# answers exactly one thing, so a question that also compares, reaches an
# external system, or asks a second question keeps the loop -- which can ask
# the obligation question as one of its sub-goals rather than losing the rest.
_NOT_A_LOOKUP = frozenset(
    {
        RoutingSignal.SEPARABLE_SUBGOALS,
        RoutingSignal.MULTIPLE_QUESTIONS,
        RoutingSignal.COMPARISON,
        RoutingSignal.EXTERNAL_SYSTEM,
    }
)


def single_lookup(text: str) -> bool:
    """Whether the question asks one thing, as a filter route needs.

    False when it also compares, reaches an external system, joins a second
    line of enquiry or asks a second question: those keep the loop.
    """
    return not signals_in(text) & _NOT_A_LOOKUP


@dataclass(frozen=True, slots=True)
class Period:
    """A span of days a question named, resolved against a clock.

    Held as day offsets rather than as a `timedelta` so that "today" means the
    day and not the last 24 hours: somebody asking at 09:00 what was asked of
    them today is not asking about yesterday evening.
    """

    #: Day boundaries back from today that the span starts at.
    days_back: int
    #: How many days wide the span is. None runs up to now.
    days_wide: int | None = None

    def bounds(self, now: datetime) -> tuple[datetime, datetime | None]:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=self.days_back
        )
        if self.days_wide is None:
            return start, None
        return start, start + timedelta(days=self.days_wide)


# Longest phrase wins, so "last week" is not read as "week".
_PERIODS: tuple[tuple[str, Period], ...] = (
    ("this morning", Period(0)),
    ("yesterday", Period(1, 1)),
    ("today", Period(0)),
    ("last week", Period(14, 7)),
    ("this week", Period(7)),
    ("past week", Period(7)),
    ("last 7 days", Period(7)),
    ("this month", Period(30)),
    ("last month", Period(60, 30)),
    ("past month", Period(30)),
)


@dataclass(frozen=True, slots=True)
class ObligationQuestion:
    """An obligation question, and the period it asked about.

    `model_calls` is zero by construction, like `RoutingDecision`: nothing in
    this module calls a model, and the provenance record asserts that rather
    than describing it.
    """

    intent: ObligationIntent
    period: Period | None = None
    model_calls: int = 0


def named_period(text: str) -> Period | None:
    """The span of days the question named, if it named one."""
    padded = _normalise(text)
    for phrase, period in _PERIODS:
        if phrase in padded:
            return period
    return None


# Questions about the assistant itself. These must never reach retrieval: the
# corpus is other people's conversations, and a search for "what can you do"
# finds whatever somebody once wrote about some OTHER tool's features -- which
# the bot then presented as its own, confidently claiming to calculate
# cryptocurrency prices because a colleague had described a project that did.
_SELF_DESCRIPTION_PATTERNS = (
    "what can you do", "what you can do", "what do you do", "who are you",
    "what are you", "how do you work", "how can you help", "what can i ask",
    "help me use", "how do i use you",
    # Asking for the command list is asking what the assistant does, however
    # long the sentence around it is.
    "list of commands", "your commands", "what commands",
    # Portuguese, because this server speaks it.
    "o que voce faz", "o que você faz", "o que voce pode", "o que você pode",
    "quem e voce", "quem é você", "quem e você", "como voce funciona",
    "como você funciona", "como posso usar",
    "lista de comandos", "seus comandos", "quais comandos",
    "o que voce consegue", "o que você consegue",
)


# Words that name the ASSISTANT'S OWN machinery. They are almost never the
# subject of a team's conversation, so a short question built around them is
# asking about the bot -- "what about Wikipedia, internet access, or MCP?".
_CAPABILITY_TERMS = frozenset({
    "wikipedia", "internet", "mcp", "serpapi", "context7", "google",
    "web access", "internet access", "web search", "search the web",
    "tools", "tool", "ferramentas", "ferramenta", "capabilities",
    "capability", "funcionalidades", "features",
    "comandos", "comando", "capacidades", "capacidade", "funcoes", "funções",
    "recursos",
})
# Addressing the bot directly.
_ADDRESS_TERMS = frozenset({
    "you", "your", "yourself", "você", "voce", "vc", "seu", "sua",
    # Plurals too: "quais sao SUAS capacidades" addresses the bot exactly as
    # "qual e SUA capacidade" does, and only one of them used to.
    "seus", "suas", "ti", "te",
    "cyberfriend", "bot",
})
_GREETINGS = frozenset({
    "hi", "hey", "hello", "oi", "ola", "olá", "bom", "dia", "boa", "tarde",
    "noite", "e", "ai", "aí", "eai", "please", "por", "favor", "pf",
})
"""Words that carry no topic. Excluded from the length bound so a polite
question is judged by what it asks, not by how it opens."""

_WORD_RE = re.compile(r"[^\W_]+(?:\s+(?:access|search|the\s+web))?")


def self_description_question(text: str) -> bool:
    """Whether this asks what the assistant is or can do.

    The first version matched a list of phrases, and missed "what tools are
    available" and "what about Wikipedia, internet access, or MCP?" -- so the
    bot searched its channels, found a colleague's crypto project, and listed
    that project's features as its own. Twice. Enumerating phrasings is the
    same keyword failure that broke tool routing: there are always more ways
    to ask than any list holds.

    So this looks for SIGNALS instead. A short question counts when it names
    the assistant's own machinery (MCP, Wikipedia, tools), or addresses the
    assistant while asking about capability. Length still bounds it: a long
    question mentioning "tools" is almost always about the team's tools.
    """
    if explicit_web_search(text) or mcp_change_request(text) or market_question(text):
        # "can you search the web for X", "can you add the context7 MCP
        # server" and "can you check BTC" address the bot and name its
        # machinery, and are requests -- to use a capability, or to be refused
        # one -- not questions about it. Each has its own answer downstream.
        return False
    lowered = " ".join(text.lower().replace("?", " ").replace(",", " ").split())
    # "oque" for "o que" is the ordinary way people type it, and every
    # Portuguese pattern below is written with the space. Normalised rather
    # than duplicated, so one spelling cannot be added to the list and the
    # other forgotten.
    lowered = re.sub(r"\boque\b", "o que", lowered)
    words = lowered.split()
    # Greetings carry no topic, so they should not consume the length budget
    # that exists to separate "what can you do" from "what can you do about
    # the deploy". "Oi, o que voce pode fazer?" is the same question as
    # "o que voce pode fazer?".
    significant = [w for w in words if w not in _GREETINGS]
    if any(pattern in lowered for pattern in _SELF_DESCRIPTION_PATTERNS):
        return len(significant) <= 12
    if len(significant) > 12:
        return False

    padded = f" {lowered} "
    names_machinery = any(f" {term} " in padded for term in _CAPABILITY_TERMS)
    addresses_bot = any(w in _ADDRESS_TERMS for w in words)
    asks_capability = any(
        w in {"can", "could", "able", "have", "access", "available", "use",
              "support", "pode", "consegue", "tem", "disponível", "disponivel"}
        for w in words
    )

    # Machinery named in a short question is about the bot on its own.
    strong = {"mcp", "wikipedia", "serpapi", "context7"}
    if any(f" {t} " in padded for t in strong):
        return True
    return names_machinery and (addresses_bot or asks_capability)


# Words that make a question about a wallet's holdings rather than about what
# anybody said. Both languages, because this server uses both.
_WALLET_TERMS = frozenset({
    "wallet", "wallets", "carteira", "carteiras", "address", "endereco",
    "endereço", "balance", "balances", "saldo", "saldos", "balanco", "balanço",
    "holdings", "holds", "tokens", "token", "quanto", "hold",
})
# Verbs that make a question about the CONVERSATION, even when an address is
# in it. "What did people say about 0x..." is a corpus question and must stay
# one; only these keep an address out of the chain lookup.
_CONVERSATION_VERBS = frozenset({
    "said", "say", "says", "mentioned", "mention", "discussed", "discuss",
    "decided", "decide", "wrote", "told", "asked",
    "disse", "disseram", "falou", "falaram", "mencionou", "mencionaram",
    "comentou", "comentaram", "discutiu", "discutiram", "decidiu", "decidiram",
})
CONVERSATION_VERBS = _CONVERSATION_VERBS
"""Public for the alert route, which is vetoed by the same words."""


@dataclass(frozen=True, slots=True)
class WalletQuestion:
    """A question the chain can answer and the corpus cannot.

    `address` is None when somebody asks about a wallet without naming one.
    That is still not a corpus question -- no channel holds a live balance --
    so it is intercepted and answered by asking for the address, rather than
    searched for and answered from whatever a colleague once wrote about
    wallets.
    """

    address: str | None


_TIME_QUESTION = re.compile(
    r"\A\s*(?:"
    # English: what is the date / time / day today
    r"(?:what(?:'s|\s+is)?|which)\s+(?:is\s+)?(?:the\s+|today'?s?\s+)*"
    r"(?:date|time|day|day\s+of\s+the\s+week)"
    r"|what\s+day\s+is\s+it"
    r"|what\s+time\s+is\s+it"
    r"|do\s+you\s+know\s+(?:what\s+)?(?:the\s+)?(?:date|time|day)"
    # Portuguese
    r"|que\s+(?:dia|horas?)\s+(?:é|e|s[ãa]o)"
    r"|qual\s+(?:é|e)?\s*(?:a\s+)?(?:data|hora)"
    r"|que\s+dia\s+(?:é|e)\s+hoje"
    r"|qual\s+o\s+dia\s+de\s+hoje"
    r")"
    r"[\s?!.,]*(?:(?:de\s+|of\s+)?(?:hoje|today|now|agora|right\s+now))?[\s?!.,]*\Z",
    re.IGNORECASE,
)
"""Asking what the date or time *is*, and nothing else.

Anchored at both ends, deliberately. "What was decided today" and "what time
did the deploy finish" are questions about the corpus that merely contain the
word; only a question whose whole content is the clock belongs here.
"""


def time_question(text: str) -> bool:
    """Whether this asks the assistant what the date or time is.

    Its own route for the same reason self-description and wallet balances
    have one: the corpus cannot answer it. Before this existed the question
    went to retrieval, found nothing, and was answered "I couldn't find
    anything about that in the messages you can see" -- which is the grounding
    rule working correctly on a question that should never have reached it.
    """
    return bool(_TIME_QUESTION.match(text))


def wallet_question(text: str) -> WalletQuestion | None:
    """The wallet lookup being asked for, or None for everything else.

    A balance is never in the corpus. A channel message about a wallet is a
    record of what somebody said, and answering "what does 0x... hold" from
    one is how the assistant ends up reporting a colleague's project summary
    as somebody's balance -- which is exactly what it did.

    An address plus conversation verbs stays with the corpus: "what did people
    say about 0x..." is a question about the conversation, and the address is
    its subject rather than a lookup.
    """
    addresses = find_addresses(text)
    words = set(text.lower().replace("?", " ").replace(",", " ").split())
    if addresses:
        if words & _CONVERSATION_VERBS:
            return None
        return WalletQuestion(address=addresses[0])
    # No address, but plainly about a wallet: intercepted so the answer is
    # "which address?" rather than a search of other people's conversations.
    names_a_wallet = {"wallet", "wallets", "carteira", "carteiras"}
    if (words & names_a_wallet) and (words & (_WALLET_TERMS - names_a_wallet)):
        return WalletQuestion(address=None)
    # "What's my balance?" and nothing else: the asker's saved wallet, or a
    # request for one. Anchored, because "my balance of vacation days" is not.
    if _MY_BALANCE.match(text):
        return WalletQuestion(address=None)
    return None


_MY_BALANCE = re.compile(
    r"\A\s*(?:"
    r"(?:what(?:'s|\s+is)|show(?:\s+me)?|check)\s+my\s+(?:current\s+)?balances?"
    r"|qual\s+(?:[ée]\s+)?(?:o\s+)?meu\s+saldo(?:\s+atual)?"
    r"|(?:mostra|mostre|ver)\s+(?:o\s+)?meu\s+saldo"
    r"|meu\s+saldo"
    r")[\s?!.,]*\Z",
    re.IGNORECASE,
)
"""A bare "what is my balance?", in either language. A total balance is the
portfolio's (`portfolio_question`), which is checked first."""


class PositionKind(StrEnum):
    LIQUIDITY = "liquidity"
    LENDING = "lending"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class DefiQuestion:
    """A question about somebody's liquidity or lending positions.

    `address` is None when none was written; the route then uses the asker's
    saved wallet, or asks for one.
    """

    kind: PositionKind
    address: str | None
    #: The address came from the asker's own earlier question, not this one.
    carried: bool = False


_LIQUIDITY_TERMS = frozenset({
    "liquidity", "liquidez", "lp", "lps", "pool", "pools", "uniswap", "range",
    "ranges", "uncollected", "univ3", "univ4", "v3", "v4",
})
# Crypto-specific words only. "My loan" or "my health" alone could be about
# anything, and a person asking about their mortgage must not be answered with
# an Aave report.
_LENDING_TERMS = frozenset({
    "aave", "borrow", "borrows", "borrowed", "borrowing", "supplied", "lending",
    "collateral", "emprestimo", "emprestimos", "empréstimo", "empréstimos",
    "colateral",
})
_GENERIC_POSITION_TERMS = frozenset({
    "defi", "position", "positions", "posicao", "posicoes", "posição", "posições",
})
# Positions are only ever somebody's: an address in the question, or the
# asker's own ("my pools"). Without either, "what did we decide about the
# pool" is a conversation about a pool and stays with the corpus.
_FIRST_PERSON = frozenset({"my", "mine", "meu", "meus", "minha", "minhas"})


def defi_question(text: str, previous: Sequence[str] = ()) -> DefiQuestion | None:
    """The positions lookup being asked for, or None for everything else.

    Checked before `wallet_question`: "what's in my pools on 0x..." names an
    address too, and it is positions that were asked about.

    `previous` is the asker's own earlier questions in this conversation,
    oldest first. A follow-up that names no address and no "my" -- "show me
    the details of the v4 position" -- is about the address asked about just
    before; without this it went to the corpus and was answered from a
    colleague's message about a different position.
    """
    words = set(re.findall(r"[\w.]+", text.lower()))
    kind = _position_kind(words)
    if kind is None or words & _CONVERSATION_VERBS:
        return None
    addresses = find_addresses(text)
    if addresses:
        return DefiQuestion(kind=kind, address=addresses[0])
    # "I" alone is too common to mean "mine"; with a protocol named it does.
    about_me = bool(words & _FIRST_PERSON) or bool(
        words & {"i", "eu"} and words & {"aave", "uniswap"}
    )
    if about_me:
        return DefiQuestion(kind=kind, address=None)
    # Carried only on a protocol or pool word: "what position did the team
    # take?" after a balance question is about the team, not the wallet.
    specific = words & (_LIQUIDITY_TERMS | _LENDING_TERMS) or {"health", "factor"} <= words
    carried = recent_chain_address(previous) if specific else None
    if carried is None:
        return None
    return DefiQuestion(kind=kind, address=carried, carried=True)


def _position_kind(words: set[str]) -> PositionKind | None:
    liquidity = bool(words & _LIQUIDITY_TERMS)
    lending = bool(words & _LENDING_TERMS) or {"health", "factor"} <= words
    if liquidity and not lending:
        return PositionKind.LIQUIDITY
    if lending and not liquidity:
        return PositionKind.LENDING
    if liquidity or lending or words & _GENERIC_POSITION_TERMS:
        return PositionKind.BOTH
    return None


_FOLLOW_UP_TURNS = 3
"""How far back a follow-up may reach for its address. A conversation that
has moved on for longer than this is not following up on that wallet."""


def recent_chain_address(previous: Sequence[str]) -> str | None:
    """The address of the latest chain question among the last few turns."""
    for earlier in reversed(previous[-_FOLLOW_UP_TURNS:]):
        addresses = _chain_turn(earlier)
        if addresses:
            return addresses[0]
    return None


def _chain_turn(text: str) -> tuple[str, ...] | None:
    """The addresses a chain question named, () for none, None if not one."""
    portfolio = portfolio_question(text)
    if portfolio is not None:
        return portfolio.addresses
    found = defi_question(text) or wallet_question(text)
    if found is None:
        return None
    return (found.address,) if found.address is not None else ()


@dataclass(frozen=True, slots=True)
class PortfolioQuestion:
    """What somebody holds in total, on chain.

    `addresses` are the ones written in the question (or, when `carried`, in
    the asker's own earlier one). `mine` says the question is about the
    asker's own money -- first person, or no address at all -- so their saved
    wallet is added to whatever they typed.
    """

    addresses: tuple[str, ...]
    mine: bool
    carried: bool = False


_PORTFOLIO_BARE = re.compile(
    r"\A\s*(?:"
    r"quanto\s+(?:eu\s+)?tenho(?:\s+(?:no\s+total|ao\s+todo|em\s+cripto))?"
    r"|quanto\s+vale\s+(?:a\s+|o\s+)?"
    r"(?:minha\s+carteira|meu\s+portf[oó]lio|meu\s+patrim[oô]nio)"
    r"|qual\s+(?:[ée]\s+)?(?:o\s+)?meu\s+(?:saldo|valor|patrim[oô]nio)\s+total"
    r"|how\s+much\s+(?:do\s+i\s+have|am\s+i\s+worth|(?:money|crypto)\s+do\s+i\s+have)"
    r"(?:\s+(?:in\s+total|overall|altogether|on[\s-]?chain))?"
    r"|what(?:'s|\s+is)\s+my\s+total\s+(?:balance|value|holdings)"
    r"|what(?:'s|\s+is)\s+my\s+(?:crypto\s+|defi\s+|on[\s-]?chain\s+)?"
    r"(?:portfolio|net\s+worth)(?:\s+worth)?(?:\s+on[\s-]?chain)?"
    r")[\s?!.,]*\Z",
    re.IGNORECASE,
)
"""The short forms, whole question only. "quanto eu tenho no total?" is also
how somebody asks about their vacation days, so a longer question with these
words is not one of these (see `_TOTAL_PHRASE` for what it then needs)."""

_PORTFOLIO_FOLLOW_UP = re.compile(
    r"\A\s*(?:e|and)\s+(?:no\s+total|ao\s+todo|o\s+total|in\s+total|the\s+total|"
    r"overall|altogether)[\s?!.,]*\Z",
    re.IGNORECASE,
)
""""e no total?" after a chain question: that wallet, summed."""

_TOTAL_PHRASE = re.compile(
    r"\b(?:in\s+total|no\s+total|ao\s+todo|total\s+(?:balance|value|holdings)|"
    r"(?:saldo|valor)\s+total|net\s+worth|worth\s+in\s+total)\b",
    re.IGNORECASE,
)
_PORTFOLIO_TERMS = frozenset({
    "portfolio", "portfólio", "portifolio", "patrimonio", "patrimônio",
})
_VALUE_TERMS = frozenset({
    "worth", "value", "vale", "valor", "total", "quanto", "much", "balance", "saldo",
})
# What makes "my total" about money on chain rather than about anything else.
_HOLDINGS_TERMS = frozenset({
    "wallet", "wallets", "carteira", "carteiras", "crypto", "cripto", "cryptos",
    "criptos", "defi", "onchain", "chain",
})


def portfolio_question(text: str, previous: Sequence[str] = ()) -> PortfolioQuestion | None:
    """The portfolio total being asked for, or None for everything else.

    Checked before the positions and balance predicates: "what's my wallet's
    total balance" names a wallet and a balance, and it is everything that
    was asked about. A question naming a pool or a loan and no portfolio word
    ("how much do I have in total in my LP") is the positions route's.
    """
    words = set(re.findall(r"[\w-]+", text.lower()))
    if words & _CONVERSATION_VERBS:
        return None
    addresses = tuple(find_addresses(text))
    if _PORTFOLIO_FOLLOW_UP.match(text):
        return _portfolio_follow_up(previous)
    if not _asks_for_a_total(text, words, bool(addresses)):
        return None
    about_me = bool(words & (_FIRST_PERSON | {"i", "eu"}))
    return PortfolioQuestion(addresses=addresses, mine=about_me or not addresses)


def _asks_for_a_total(text: str, words: set[str], addressed: bool) -> bool:
    if _PORTFOLIO_BARE.match(text):
        return True
    named = bool(words & _PORTFOLIO_TERMS)
    if not named and _position_kind(words) is not None:
        return False
    if addressed:
        return named or bool(_TOTAL_PHRASE.search(text))
    if not words & (_FIRST_PERSON | {"i", "eu"}):
        return False
    if named:
        return bool(words & _VALUE_TERMS)
    return bool(words & _HOLDINGS_TERMS) and bool(_TOTAL_PHRASE.search(text))


def _portfolio_follow_up(previous: Sequence[str]) -> PortfolioQuestion | None:
    for earlier in reversed(previous[-_FOLLOW_UP_TURNS:]):
        addresses = _chain_turn(earlier)
        if addresses is None:
            continue
        if addresses:
            return PortfolioQuestion(addresses=addresses, mine=False, carried=True)
        return PortfolioQuestion(addresses=(), mine=True)
    return None


def obligation_question(text: str) -> ObligationQuestion | None:
    """The obligation question being asked, or None for everything else.

    None is the common case and the safe one: it leaves the question on the
    retrieval paths, which is where every question went before this existed.
    """
    padded = _normalise(text)
    if signals_in(text) & _NOT_A_LOOKUP:
        return None
    if any(term in padded for term in _ASKED_OF_ME_TERMS):
        return ObligationQuestion(ObligationIntent.ASKED_OF_ME, named_period(text))
    if _SELF_REFERENCE.search(text) and any(
        term in padded for term in _MY_OBLIGATION_TERMS
    ):
        return ObligationQuestion(ObligationIntent.MY_OBLIGATIONS, named_period(text))
    return None


# --- decision questions ---------------------------------------------------
#
# "O que decidimos sobre o deploy?" is answered from the `decision` rows, as an
# obligation question is from the `ask` rows, and claimed here for the same
# reasons: lexically, with no model call, and narrowly. A question this misses
# is answered by retrieval as before. One it claims and finds nothing for is
# handed back to retrieval too, so a wrong claim costs an embedding, never a
# "nothing was decided".
#
# Every shape is about what a group settled: "we", "a gente", the passive.
# "What did you decide" asks the bot, "o que o João decidiu" asks about one
# person, and "decide between A and B" asks for help deciding; none of them is
# a lookup of the log.


@dataclass(frozen=True, slots=True)
class DecisionQuestion:
    """A decision question, what it was about, and when.

    `topic` is as typed ("o deploy"), empty when none was named. `span` is
    None when no time was named. `language` is the shape's, which the reply is
    written in. `model_calls` is zero by construction, like `RoutingDecision`.
    """

    topic: str
    span: Span | None
    language: Language
    model_calls: int = 0


_DECISION_END = r"\s*[?.!]*\s*$"
_PT_WHAT = r"(?:e\s+)?(?:o\s*que|oq|o\s+q)(?:\s+(?:e\s+)?que)?"
_PT_WE = r"(?:(?:nos|a\s+gente|agente|o\s+time|a\s+equipe|o\s+pessoal|a\s+galera)\s+)?"
_PT_DECIDED = (
    r"(?:decidimos|decidiram|decidiu|combinamos|combinaram|definimos|definiram"
    r"|fechamos|acordamos|se\s+decidiu"
    r"|(?:foi|foram|ficou|ficaram|tinha\s+ficado)\s+"
    r"(?:decidid|combinad|definid|acordad|fechad)[oa]s?)"
)
_PT_DECISION = r"decis(?:ao|oes)(?:\s+tomadas?)?"
_PT_TOPIC = (
    r"(?:\s+(?:sobre|a\s+respeito\s+d[aeo]s?|acerca\s+d[aeo]s?|(?:em|com)\s+relacao\s+a[os]?"
    r"|quanto\s+a[os]?|pr[ao]s?|para\s+[ao]s?|d[aeo]s?|n[ao]s?)\s+(?P<topic>.+?))?"
)
_EN_WE = r"(?:we|the\s+team|the\s+group|everyone)"
_EN_DECIDED = r"(?:decide|decided|agree|agreed|settle|settled)(?:\s+to\s+do)?"
_EN_TOPIC = (
    r"(?:\s+(?:(?:on|upon)\s+)?(?:about|on|upon|regarding|concerning|for|with)"
    r"\s+(?P<topic>.+?)|\s+(?:on|upon))?"
)
_EN_DECISION = r"decisions?(?:\s+(?:we\s+made|made|taken))?"

_DECISION_SHAPES: tuple[tuple[re.Pattern[str], Language], ...] = tuple(
    (re.compile(rf"^{head}{topic}{_DECISION_END}"), language)
    for head, topic, language in (
        # "o que decidimos (sobre Y)", "o que ficou decidido do Y", "o que a gente combinou"
        (rf"{_PT_WHAT}\s+{_PT_WE}{_PT_DECIDED}", _PT_TOPIC, Language.PORTUGUESE),
        # "qual foi a decisão sobre Y", "quais as decisões do Y"
        (
            rf"(?:e\s+)?(?:qual|quais)(?:\s+(?:foi|foram|e|sao|era|eram))?"
            rf"(?:\s+(?:a|as))?\s+{_PT_DECISION}",
            _PT_TOPIC,
            Language.PORTUGUESE,
        ),
        # "que decisões tomamos sobre Y", "quais decisões foram tomadas"
        (
            r"(?:e\s+)?(?:que|quais)\s+decisoes\s+"
            r"(?:tomamos|tomaram|a\s+gente\s+tomou|foram\s+tomadas)",
            _PT_TOPIC,
            Language.PORTUGUESE,
        ),
        # "teve alguma decisão sobre Y", "houve decisões sobre Y"
        (
            rf"(?:e\s+)?(?:teve|tem|houve|ha|rolou)\s+(?:alguma\s+)?{_PT_DECISION}",
            _PT_TOPIC,
            Language.PORTUGUESE,
        ),
        # "decisões sobre Y?" -- only with a topic, which the shape requires.
        (r"(?:as\s+)?decisoes", _PT_TOPIC.removesuffix("?"), Language.PORTUGUESE),
        # "what did we decide (about Y)", "what have we agreed on"
        (
            rf"(?:so\s+|and\s+)?what\s+(?:did|have|had)\s+{_EN_WE}\s+{_EN_DECIDED}",
            _EN_TOPIC,
            Language.ENGLISH,
        ),
        # "what was decided (about Y)", "what has been agreed on Y"
        (
            r"(?:so\s+|and\s+)?what\s+(?:was|were|has\s+been|have\s+been|got|had\s+been)\s+"
            r"(?:decided|agreed|settled)",
            _EN_TOPIC,
            Language.ENGLISH,
        ),
        # "what was the decision on Y", "what were our decisions about Y"
        (
            rf"(?:so\s+|and\s+)?what(?:'s|\s+is|\s+was|\s+were|\s+are)\s+(?:the|our)\s+{_EN_DECISION}",
            _EN_TOPIC,
            Language.ENGLISH,
        ),
        # "what decisions did we make about Y", "which decisions were made"
        (
            r"(?:what|which)\s+decisions\s+(?:did\s+we\s+(?:make|take)|have\s+we\s+(?:made|taken)"
            r"|were\s+(?:made|taken)|have\s+been\s+made)",
            _EN_TOPIC,
            Language.ENGLISH,
        ),
        # "did we decide anything about Y" -- only with a topic.
        (
            rf"(?:did|have)\s+{_EN_WE}\s+{_EN_DECIDED}(?:\s+(?:anything|something))?",
            _EN_TOPIC.removesuffix("?"),
            Language.ENGLISH,
        ),
        # "were there any decisions about Y", "any decisions on Y"
        (
            r"(?:(?:were|was)\s+there\s+)?any\s+decisions?(?:\s+(?:made|taken))?",
            _EN_TOPIC,
            Language.ENGLISH,
        ),
        # "decisions about Y?" -- only with a topic.
        (r"(?:the\s+)?decisions", _EN_TOPIC.removesuffix("?"), Language.ENGLISH),
    )
)

#: Asking for help choosing, not for what was chosen.
_CHOOSING = re.compile(r"\b(?:between|entre)\b")

#: A topic that is a pronoun needs the conversation to resolve it, which
#: retrieval has and this route does not.
_PRONOUN_TOPICS = frozenset(
    {"it", "that", "this", "those", "these", "isso", "isto", "aquilo", "ele", "ela", "esse",
     "essa", "este", "esta"}
)


def _decision_deferred(text: str, folded: str) -> bool:
    """Whether the question asks for more than one lookup, or belongs elsewhere."""
    return (
        not single_lookup(text)
        or _CHOOSING.search(folded) is not None
        or market_question(text) is not None
        or fact_intent(text) is not None
    )


def _decision_shape(folded: str) -> tuple[re.Match[str], Language] | None:
    for pattern, language in _DECISION_SHAPES:
        match = pattern.match(folded)
        if match is not None:
            return match, language
    return None


def decision_question(text: str, now: datetime, tz: tzinfo) -> DecisionQuestion | None:
    """The decision question being asked, or None for everything else.

    `now` and `tz` resolve the time it names ("semana passada" is Sao Paulo's
    last calendar week), through `timespan`, as said-by resolves its own. A
    time that cannot be read as one span is None rather than dropped, for the
    reason `timespan.cut_span` gives.
    """
    if _decision_deferred(text, fold(text)):
        return None
    cut = cut_span(text, now, tz)
    if cut is None:
        return None
    found = _decision_shape(cut.folded)
    if found is None:
        return None
    match, language = found
    typed = cut.typed[match.start("topic") : match.end("topic")] if match["topic"] else ""
    topic = cut_topic(typed)
    if fold(topic) in _PRONOUN_TOPICS:
        return None
    return DecisionQuestion(topic, cut.span, language)


# --- questions that must not be answered from the corpus ----------------
#
# Three kinds of question are decided here before either retrieval path runs,
# each because the corpus is the wrong instrument for it rather than merely a
# weaker one:
#
# *   **Current market figures.** A channel message quoting a price is a record
#     of what somebody said, not a price. Retrieval ranks "BTC is at 60k" from
#     last month highly for "what is BTC at", and the answer then presents a
#     stale figure as current -- on a server that discusses crypto, the most
#     likely wrong answer this bot can give.
# *   **An explicit web search.** Somebody who says "search the web for" has
#     already told us the corpus is not what they want; answering from it
#     instead is not a fallback, it is ignoring the request.
# *   **Operator actions asked for in chat.** Connecting an MCP server widens
#     what the agent can reach, and indexing a channel makes it permanent. Both
#     have a front door that checks who is asking; a chat message is not it.
#
# Lexical, like everything above, and for the same reason. The cost of a miss
# differs by kind: a missed price question falls back to the corpus, which is
# exactly the failure being prevented, so these lean inclusive; a missed web
# request is answered from the corpus first and may still escalate.


class MarketKind(StrEnum):
    CRYPTO = "crypto"
    INDEX = "index"
    CONVERSION = "conversion"


@dataclass(frozen=True, slots=True)
class MarketQuestion:
    """A question for a current market figure.

    `advisory` marks "should I buy BTC": it still gets the figure, and it gets
    a plain statement that no recommendation is made instead of one.
    """

    kind: MarketKind
    advisory: bool = False


_CRYPTO_TERMS = frozenset({"btc", "bitcoin", "bitcoins", "eth", "ether", "ethereum"})
_INDEX_PATTERN = re.compile(
    r"s\s*&\s*p(?:\s*500)?|\bs\s+and\s+p(?:\s*500)?\b|\bsp\s?500\b|\bspx\b"
)
_PRICE_TERMS = (
    " price", " prices", " priced", " worth", " trading", " quote", " value of",
    " how much", " cost", " going for", " at now", " at right now", " right now",
    " currently", " current", " today", " level", " where is", " where's",
    " cotação", " cotacao", " preço", " preco", " valor", " quanto", " hoje",
    " agora", " atual",
)
_ADVICE_PATTERN = re.compile(
    r"\b(should i|shall i|is it a good time|good time to|worth buying|"
    r"buy or sell|invest in|would you buy|recommend|devo|vale a pena|"
    r"comprar|vender)\b"
)
_ADVICE_VERBS = frozenset({"buy", "sell", "hold", "invest", "short", "long"})

# Conversation and past-time markers. "What did Ana say BTC would hit" and
# "what was BTC at last month" are questions about a record, and the corpus is
# the right place to look; only the present tense of a price leaves it.
_RECORD_MARKERS = frozenset({
    "said", "say", "says", "mentioned", "posted", "wrote", "discussed",
    "discussion", "talked", "predicted", "prediction", "decided", "decide",
    "was", "were", "yesterday", "last", "ago", "channel", "thread", "disse",
    "falou", "ontem", "passado", "passada", "foi", "estava", "era",
})
# "semana" and "mês" are not markers on their own: "quanto está o bitcoin essa
# semana" asks for the figure now, and the past it would guard against is
# already carried by "passada", "passado" or a past-tense verb.

# An explicit claim on the present outranks the record markers. "What is the
# current BTC price? I was away" carries a past tense that is about the asker,
# not the figure, and treating it as a record question hands a stale channel
# quote back as the price. Only words that date the figure itself qualify:
# "today" and "at the moment" do not ("what did Ana say about BTC today",
# "what was BTC at the moment she posted" are about a message).
_PRESENT_MARKERS = frozenset({
    "current", "currently", "now", "presently", "atual", "atualmente", "agora",
    "nowadays",
})
_PRESENT_PHRASES = (" these days ", " hoje em dia ")

# An amount of an instrument, which with a currency beside it is a conversion
# of a price: "how many dollars is one bitcoin", "1 BTC in BRL". A currency
# alone is not enough -- "we accept BTC and USD" names both and asks nothing.
_AMOUNT_PATTERN = re.compile(r"\b(\d+(?:[.,]\d+)?|one|a single|um|uma|how many|quantos)\b")

_CURRENCY_NAMES = {
    "dollar": "USD", "dollars": "USD", "dólar": "USD", "dolar": "USD",
    "dólares": "USD", "dolares": "USD", "euro": "EUR", "euros": "EUR",
    "real": "BRL", "reais": "BRL", "pound": "GBP", "pounds": "GBP",
    "sterling": "GBP", "yen": "JPY", "iene": "JPY", "libra": "GBP", "libras": "GBP",
}
# Codes spelled in lower case are counted only when they are unmistakably
# currencies; the rest of ISO 4217 counts only as written in capitals, so
# "all", "top" and "try" in a sentence are words.
_COMMON_CODES = frozenset({
    "usd", "eur", "brl", "gbp", "jpy", "cad", "aud", "chf", "cny", "ars", "mxn",
})
_CONVERSION_TERMS = (
    " convert", " conversion", " exchange rate", " exchange", " in ", " to ",
    " into ", " rate", " em ", " para ", " câmbio", " cambio", " converter",
    " cotação", " cotacao", " how much",
)
_CODE_PATTERN = re.compile(r"\b[A-Z]{3}\b")


def _currencies_in(text: str, iso_codes: frozenset[str]) -> set[str]:
    found = {c for c in _CODE_PATTERN.findall(text) if c in iso_codes}
    for word in re.findall(r"[^\W\d_]+", text.lower()):
        if word in _COMMON_CODES:
            found.add(word.upper())
        elif word in _CURRENCY_NAMES:
            found.add(_CURRENCY_NAMES[word])
    return found


def asks_for_advice(text: str) -> bool:
    """Whether the question asks what to do with an asset, not what it costs."""
    lowered = text.lower()
    if _ADVICE_PATTERN.search(lowered):
        return True
    words = set(re.findall(r"[^\W\d_]+", lowered))
    return bool(words & _ADVICE_VERBS) and ("i" in words or "we" in words)


def market_question(
    text: str, iso_codes: frozenset[str] = frozenset()
) -> MarketQuestion | None:
    """The market figure a question asks for now, or None.

    `iso_codes` is the currency vocabulary the conversion tool accepts. It is
    handed in rather than imported so routing keeps no copy of it: a code the
    tool would refuse is not a conversion this can recognise either.
    """
    lowered = " " + " ".join(text.lower().split()) + " "
    words = set(re.findall(r"[^\W\d_]+", lowered))
    if _about_the_record(lowered, words):
        return None
    advisory = asks_for_advice(text)
    priced = advisory or any(term in lowered for term in _PRICE_TERMS)
    short = len(lowered.split()) <= 4
    currencies = _currencies_in(text, iso_codes)
    converting = any(term in lowered for term in _CONVERSION_TERMS)
    # A crypto amount beside a currency is a price asked for in that currency.
    in_a_currency = bool(currencies) and (converting or bool(_AMOUNT_PATTERN.search(lowered)))

    if words & _CRYPTO_TERMS and (priced or short or in_a_currency):
        return MarketQuestion(MarketKind.CRYPTO, advisory)
    if _INDEX_PATTERN.search(lowered) and (priced or short):
        return MarketQuestion(MarketKind.INDEX, advisory)
    if len(currencies) >= 2 and converting:
        return MarketQuestion(MarketKind.CONVERSION, advisory)
    if currencies and (" exchange rate" in lowered or " câmbio" in lowered
                       or " cambio" in lowered):
        return MarketQuestion(MarketKind.CONVERSION, advisory)
    return None


def _about_the_record(lowered: str, words: set[str]) -> bool:
    """Whether a question is about what was said rather than the figure now."""
    if not words & _RECORD_MARKERS:
        return False
    # Punctuation folded to spaces, so "these days?" is the phrase it reads as.
    bare = " " + " ".join(re.sub(r"[^\w\s]", " ", lowered).split()) + " "
    present = words & _PRESENT_MARKERS or any(p in bare for p in _PRESENT_PHRASES)
    return not present


# A follow-up to a price question names no instrument: "and now?", "ok but what
# is it trading at", "e agora?". Read alone it is nothing, and with memory
# present it went to the loop, whose planner turned it into a corpus search for
# the price -- the stale channel quote again, one turn later. So it is
# recognised here, against the asker's previous question, and needs both a
# continuation cue and a reason to think the figure is still what is wanted.
_FOLLOW_UP_OPENERS = (
    " and ", " ok ", " okay ", " so ", " but ", " what about ", " how about ", " e ",
    " mas ", " então ", " entao ", " e quanto ",
)
_FOLLOW_UP_PRONOUNS = frozenset({"it", "that", "this", "ele", "isso", "esse", "este"})
_FOLLOW_UP_REFRESH = frozenset({
    "again", "update", "updated", "latest", "still", "novamente", "denovo", "ainda",
})
_FOLLOW_UP_MAX_WORDS = 8

_CRYPTO_CANONICAL = {
    "btc": "BTC", "bitcoin": "BTC", "bitcoins": "BTC",
    "eth": "ETH", "ether": "ETH", "ethereum": "ETH",
}


@dataclass(frozen=True, slots=True)
class MarketFollowUp:
    """A follow-up to a price question, and what it follows up on.

    `subject` is built only from closed-vocabulary terms and numbers found in
    the asker's earlier question -- "BTC price", "100 USD to BRL" -- never the
    remembered text itself. Remembered turns are data: what reaches the tool
    proposal from one is a term the market tools' own vocabulary would accept,
    and nothing a sentence could be smuggled in.
    """

    question: MarketQuestion
    subject: str

    def text_for(self, follow_up: str) -> str:
        return f"{follow_up.strip()} (following up on: {self.subject})"


def market_follow_up(
    text: str,
    previous_questions: Sequence[str],
    iso_codes: frozenset[str] = frozenset(),
) -> MarketFollowUp | None:
    """The market question `text` continues, or None.

    `previous_questions` are the asker's own earlier questions, oldest first.
    The newest one must be the price question, or a follow-up to it: "what is
    BTC at", "and now?", "and now?" is one line of questioning, while a price
    asked before an unrelated question is not what "and now?" refers to once
    the conversation has moved on.
    """
    if not _follows_up(text, iso_codes):
        return None
    for earlier_text in reversed(previous_questions):
        earlier = market_question(earlier_text, iso_codes)
        if earlier is not None:
            subject = _market_subject(earlier_text, earlier.kind, iso_codes)
            return MarketFollowUp(MarketQuestion(earlier.kind, asks_for_advice(text)), subject)
        if not _follows_up(earlier_text, iso_codes):
            return None
    return None


def _follows_up(text: str, iso_codes: frozenset[str]) -> bool:
    """Whether a question is shaped like a follow-up still wanting the figure."""
    lowered = " " + " ".join(text.lower().split()) + " "
    words = set(re.findall(r"[^\W\d_]+", lowered))
    if len(lowered.split()) > _FOLLOW_UP_MAX_WORDS or _about_the_record(lowered, words):
        return False
    continues = (
        any(lowered.startswith(opener) for opener in _FOLLOW_UP_OPENERS)
        or bool(words & _FOLLOW_UP_PRONOUNS)
    )
    still_wanted = (
        bool(words & (_PRESENT_MARKERS | _FOLLOW_UP_REFRESH))
        or any(term in lowered for term in _PRICE_TERMS)
        or bool(_currencies_in(text, iso_codes))
    )
    return continues and still_wanted


def _market_subject(text: str, kind: MarketKind, iso_codes: frozenset[str]) -> str:
    """The instrument an earlier question named, in closed-vocabulary terms."""
    if kind is MarketKind.INDEX:
        return "S&P 500 level"
    terms: list[str] = []
    for token in re.findall(r"\d+(?:[.,]\d+)?|[^\W\d_]+", text):
        term = _subject_term(token, kind, iso_codes)
        if term is not None and term not in terms:
            terms.append(term)
    if kind is MarketKind.CRYPTO:
        return " ".join([*terms, "price"])
    return " ".join(terms) + " exchange rate"


def _subject_term(token: str, kind: MarketKind, iso_codes: frozenset[str]) -> str | None:
    lowered = token.lower()
    if kind is MarketKind.CRYPTO:
        return _CRYPTO_CANONICAL.get(lowered)
    if token[0].isdigit():
        return token
    found = _currencies_in(token, iso_codes)
    return next(iter(found)) if found else None


# Each alternative needs a verb aimed at the web AND something to look for.
# "can you search the web?" names the capability and asks about the assistant;
# "search the web for the latest Python release" is the request.
_WEB_REQUEST_PATTERN = re.compile(
    r"\b(search|look\s+up|check|browse)\s+(on\s+)?(the\s+)?(web|internet)\s+"
    r"(for|about|on|to\s+find)\s+\S|"
    r"\b(search|look\s+up)\s+\S.{0,80}\s+(online|on\s+the\s+(web|internet))\b|"
    r"\b(search|look\s+up|check)\s+online\s+(for\s+)?\S|"
    r"\bweb\s+search\s+(for|on|about)\s+\S|"
    r"^\s*(please\s+)?google\s+(for\s+)?\S|"
    r"\b(pesquis\w*|procur\w*|busc\w*)\s+(na\s+)?(web|internet)\s+(por|sobre)\s+\S"
)


def explicit_web_search(text: str) -> bool:
    """Whether the person asked, in words, for the web rather than the corpus.

    Requires a verb aimed at the web ("search the web for", "look up online",
    "google X"); naming the web alone is not a request -- "do you have
    internet access" asks about the assistant, and is answered as such.
    """
    return bool(_WEB_REQUEST_PATTERN.search(text.lower()))


_MCP_CHANGE_PATTERN = re.compile(
    r"\b(add|connect|install|configure|register|enable|set\s+up|setup|attach|"
    r"plug\s+in|hook\s+up|use\s+a\s+new|remove|disconnect|disable|uninstall|"
    r"adicion\w*|conect\w*|instal\w*|configur\w*|remov\w*)\b"
    r".{0,60}\b(mcp|model\s+context\s+protocol)\b"
    r"|\b(mcp|model\s+context\s+protocol)\b.{0,40}\b(server|servidor)\b.{0,40}"
    r"\b(add|connect|install|configure|register|enable|remove|disconnect)\b"
)


def mcp_change_request(text: str) -> bool:
    """Whether chat is asking the assistant to add, change or remove an MCP server.

    Deliberately wider than it needs to be. A false positive tells somebody
    where the admin console is; a false negative lets a question about wiring
    new reach into the agent be answered as if chat could do it.
    """
    return bool(_MCP_CHANGE_PATTERN.search(" ".join(text.lower().split())))


_INDEX_REQUEST_PATTERN = re.compile(
    r"^\s*(please\s+)?(/)?(un)?index\b|"
    r"\b(start|stop)\s+(indexing|archiving)\b|"
    r"\b(add|remove)\b.{0,40}\bto\s+(the\s+)?(index|indexing|archive)\b|"
    r"\b(index|unindex|archive)\s+(this\s+channel|#|<#)"
)


def indexing_request(text: str) -> bool:
    """Whether chat is asking the assistant to index or unindex a channel.

    Chat cannot do that, whoever is asking and whatever they say about
    themselves: `/index` resolves the requester's Manage Channels permission
    from the guild. Recognising the request here only means the answer is a
    pointer to the command, instead of a search of the corpus for the word
    "index".
    """
    return bool(_INDEX_REQUEST_PATTERN.search(text.lower()))


# --- personal facts ---------------------------------------------------------
#
# The person's own message to the assistant is the only place a fact may come
# from. That is enforced by where this runs -- on `AskRequest.text`, before
# anything is recalled or retrieved -- and by what it recognises: statements in
# the first person. A fact stated about someone else is recognised too, but only
# so it can be refused by name rather than searched for.
#
# Every pattern is anchored to the whole message. "call me Leo" is a request;
# "why does everyone call me Leo in #general?" is a question, and storing
# "Leo in #general?" as a name is the false positive the design warns about.


class FactAction(StrEnum):
    SET = "set"
    # Several facts in one message: an introduction.
    SET_MANY = "set_many"
    # "what can you remember about me?": the list of what may be kept.
    CAPABILITIES = "capabilities"
    SHOW = "show"
    FORGET = "forget"
    # Something to remember that is not one of the three facts.
    UNSUPPORTED = "unsupported"
    # "João's email is ...": a fact about somebody other than the speaker.
    ABOUT_SOMEONE_ELSE = "about_someone_else"
    # "what's João's email?": a request for somebody else's facts.
    OTHERS_FACTS = "others_facts"


@dataclass(frozen=True, slots=True)
class FactIntent:
    """What the asker's message asks of their personal facts.

    `kind` None on FORGET means every fact. `value` is the raw text the person
    gave, unvalidated: validation belongs to `ports.facts.PersonalFact`, so a
    malformed email is still a SET and comes back as a refusal saying why.
    """

    action: FactAction
    kind: FactKind | None = None
    value: str | None = None
    #: SET_MANY only: each fact stated, in the order it was stated.
    sets: tuple[tuple[FactKind, str], ...] = ()
    #: SET_MANY only: what was stated but is not a fact kept (`AGE`,
    #: `WHERE_YOURE_FROM`).
    not_kept: tuple[str, ...] = ()
    #: FORGET only: "forget my wallets" -- every saved wallet, of both chains.
    all_wallets: bool = False


_POLITE = (
    r"(?:please|pls|por\s+favor|can\s+you|could\s+you|would\s+you|will\s+you|"
    r"pode|voc[eê]\s+pode)"
)
_GREETING = r"(?:hey|hi|hello|ok|okay|so|oi|ol[aá])"
_REMEMBER = r"(?:remember|note|save|keep\s+in\s+mind|lembre(?:-se)?|lembra|anota|guarda|salva)"
_LEAD = (
    rf"^(?:{_GREETING}\b[,!.]?\s*)*(?:{_POLITE}\b,?\s*)*"
    rf"(?:{_REMEMBER}\b(?:\s+(?:that|que|de\s+que))?[:,]?\s*)?"
)
_TRAIL = r"(?:,?\s*(?:please|pls|por\s+favor|thanks|thank\s+you|obrigad[oa]))?[\s.!?]*$"
_IS = r"(?:is|[ée])"

_EMAIL_WORD = r"e-?mail(?:\s+address)?"
_NAME_WORD = r"(?:preferred\s+name|nickname|name|nome(?:\s+preferido)?|apelido)"
_LANGUAGE_WORD = r"(?:preferred\s+language|language|idioma(?:\s+preferido)?|l[ií]ngua)"
_MY = r"(?:my|meu|minha)"
# Plural too, for "forget my wallets" and "esqueça minhas carteiras".
_MY_ANY = r"(?:my|meu|minha|meus|minhas)"
_PHONE_WORD = (
    r"(?:phone(?:\s+number)?|mobile|cell(?:\s*phone)?|whatsapp|"
    r"telefone|celular|n[uú]mero(?:\s+de\s+telefone)?)"
)
# "wallet" alone means the Ethereum one: it is the chain this deployment can
# read balances on, and somebody who says "my wallet is 0x..." means that.
# "walet" is the production typo that left a wallet unsaved.
_WALLET = r"wall?ets?"
_ETH_WALLET_WORD = (
    rf"(?:(?:eth(?:ereum)?|evm|base)\s+(?:{_WALLET}|address(?:es)?|endere[cç]os?)|"
    rf"{_WALLET}|carteiras?)"
)
_BTC_WALLET_WORD = (
    rf"(?:(?:btc|bitcoin)\s+(?:{_WALLET}|address(?:es)?|endere[cç]os?)|"
    rf"(?:carteiras?|{_WALLET})\s+(?:btc|bitcoin))"
)
_HOME_ADDRESS_WORD = r"(?:(?:home\s+)?address|endere[cç]o(?:\s+residencial)?)"
_HOME_ADDRESS_ASKED = (
    r"(?:home\s+address|endere[cç]o\s+(?:residencial|de\s+casa)|residential\s+address)"
)
_BIRTH_DATE_WORD = (
    r"(?:birth\s*date|date\s+of\s+birth|birthday|data\s+de\s+nascimento|"
    r"anivers[aá]rio)"
)


def _is_or_shaped(shape: str) -> str:
    """"is"/"é", a colon, or nothing when the value itself has the right shape.

    "meu email leo@example.com" states an email as plainly as "meu email é
    ...", and was answered as a question. Without the verb the value must look
    like its kind, so "my email bounced" is not a statement -- and must not be
    a question: "minha carteira 0x...?" asks about the wallet, it does not save
    it.
    """
    return rf"(?:\s*:?\s+{_IS}|\s*:|(?=\s+{shape}(?!.*\?)))"


_EMAIL_SHAPE = r"[^\s@]+@"
_PHONE_SHAPE = r"\+?\(?\d"
_ETH_SHAPE = r"0x[0-9a-f]"
_BTC_SHAPE = r"(?:bc1|[13])[a-z0-9]{20}"
# A date that starts with a digit ("21/06/1981") or a month name ("June 21").
_DATE_SHAPE = r"(?:\d|[a-zç]+\.?\s+\d)"

_SET_PATTERNS: tuple[tuple[FactKind, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(_LEAD + body + _TRAIL, re.IGNORECASE))
    for kind, body in (
        (
            FactKind.PREFERRED_NAME,
            r"(?:(?:you\s+can\s+|just\s+)?call\s+me|my\s+preferred\s+name\s+is|"
            r"i\s+(?:prefer|like|want)\s+to\s+be\s+called|"
            r"(?:set|change|update)\s+my\s+(?:preferred\s+)?name\s+to|"
            r"(?:me\s+chame|me\s+chama|me\s+chamem|pode\s+me\s+chamar)\s+de|"
            r"meu\s+nome\s+preferido\s+[ée]|prefiro\s+ser\s+chamad[oa]\s+de)"
            r"\s+(?P<value>.+?)",
        ),
        (
            FactKind.FULL_NAME,
            r"(?:my\s+full\s+name\s+is|(?:set|change|update)\s+my\s+full\s+name\s+to|"
            r"meu\s+nome\s+completo\s+[ée])"
            r"\s+(?P<value>.+?)",
        ),
        (
            FactKind.EMAIL,
            rf"(?:{_MY}\s+{_EMAIL_WORD}{_is_or_shaped(_EMAIL_SHAPE)}|"
            rf"(?:set|change|update)\s+my\s+{_EMAIL_WORD}\s+to|"
            r"(?:mude|muda|altere|atualize)\s+(?:o\s+)?meu\s+e-?mail\s+para)"
            r"\s+(?P<value>\S+?)",
        ),
        (FactKind.EMAIL, rf"use\s+(?P<value>\S+)\s+as\s+my\s+{_EMAIL_WORD}"),
        (
            FactKind.PREFERRED_LANGUAGE,
            r"(?:my\s+(?:preferred\s+)?language\s+is|"
            r"(?:set|change|update)\s+my\s+(?:preferred\s+)?language\s+to|"
            r"(?:(?:always|from\s+now\s+on,?)\s+)?(?:reply|answer|respond|speak|talk|write)"
            r"\s+to\s+me\s+in|"
            r"(?:always|from\s+now\s+on,?)\s+(?:reply|answer|respond|speak|talk|write)\s+in|"
            r"meu\s+idioma\s+(?:preferido\s+)?[ée]|"
            r"(?:sempre\s+)?(?:responda|fale|escreva)(?:\s+(?:comigo|para\s+mim|pra\s+mim))?\s+em)"
            r"\s+(?P<value>.+?)(?:\s+from\s+now\s+on)?",
        ),
        (
            FactKind.PREFERRED_LANGUAGE,
            r"(?:reply|answer|respond|speak|talk|write)(?:\s+to\s+me)?\s+in\s+(?P<value>.+?)"
            r"\s+from\s+now\s+on",
        ),
        (
            FactKind.PHONE,
            rf"(?:{_MY}\s+{_PHONE_WORD}{_is_or_shaped(_PHONE_SHAPE)}|"
            rf"(?:set|change|update)\s+my\s+{_PHONE_WORD}\s+to|"
            rf"(?:mude|muda|altere|atualize)\s+(?:o\s+)?meu\s+{_PHONE_WORD}\s+para)"
            r"\s+(?P<value>.+?)",
        ),
        # Bitcoin before Ethereum: "my btc wallet is ..." also matches the
        # Ethereum pattern's bare "wallet", and the first match wins.
        (
            FactKind.BTC_WALLET,
            rf"(?:{_MY}\s+{_BTC_WALLET_WORD}{_is_or_shaped(_BTC_SHAPE)}|"
            rf"(?:set|change|update)\s+my\s+{_BTC_WALLET_WORD}\s+to)"
            r"\s+(?P<value>\S+?)",
        ),
        (
            FactKind.ETH_WALLET,
            rf"(?:{_MY}\s+{_ETH_WALLET_WORD}{_is_or_shaped(_ETH_SHAPE)}|"
            rf"(?:set|change|update)\s+my\s+{_ETH_WALLET_WORD}\s+to|"
            rf"(?:add|save)\s+(?:my\s+|this\s+|another\s+)?{_ETH_WALLET_WORD}:?|"
            r"(?:adicione|adiciona|salve|salva)\s+(?:a\s+|minha\s+|essa\s+|outra\s+)?carteira:?|"
            rf"(?:mude|muda|altere|atualize)\s+(?:a\s+)?minha\s+carteira\s+para)"
            r"\s+(?P<value>\S+?)",
        ),
        (
            FactKind.HOME_ADDRESS,
            rf"(?:{_MY}\s+{_HOME_ADDRESS_WORD}\s+{_IS}|"
            # "morro" is the production typo of "moro"; it is also Portuguese
            # for "hill", so it needs the preposition "moro" takes.
            r"(?:eu\s+)?(?:moro|morro|resido)\s+(?:n[oa]s?|em)|"
            # English "live in" is also "I live in fear of liquidation": a
            # place is capitalised or starts with a number.
            r"i\s+live\s+(?:in|at|on)(?=\s+(?-i:[A-Z0-9\u00c0-\u00dd])))"
            r"\s+(?P<value>.+?)",
        ),
        (
            FactKind.BIRTH_DATE,
            rf"(?:{_MY}\s+{_BIRTH_DATE_WORD}\s+{_IS}|"
            # A date, as for "born": "nasci em São Paulo" is a birthplace.
            r"(?:(?:eu\s+)?nasci|nascid[oa])(?:\s+(?:em|no\s+dia|dia|a))?"
            rf"(?=\s+{_DATE_SHAPE})|"
            rf"(?:i\s+was\s+)?born(?:\s+on(?:\s+the)?|\s+in)?(?=\s+{_DATE_SHAPE}))"
            r"\s+(?P<value>.+?)",
        ),
    )
)

# A name someone asks to be called is a few words. The bound and the stop
# words are what keep "call me when the deploy is done" out of the name field:
# a miss here is answered as an ordinary question, which is recoverable.
_MAX_NAME_WORDS = 4
_MAX_FULL_NAME_WORDS = 8
_MAX_LANGUAGE_WORDS = 3
_MAX_ADDRESS_WORDS = 30
_NOT_A_NAME_START = frozenset({
    "when", "if", "after", "before", "back", "later", "tomorrow", "today",
    "tonight", "at", "on", "in", "about", "once", "whenever", "as", "by", "and",
    "or", "to", "anytime", "maybe", "asap", "now", "sometime", "quando", "se",
    "depois", "amanhã", "amanha", "mais", "hoje", "agora",
})
_QUOTES = "\"'`“”‘’"

def _wallet_named(shape: str) -> str:
    """The address itself as the value, or a suffix such as "…45e0"."""
    return rf"(?:\s+(?P<value>{shape}\S*?)|\s+(?:…|\.{{2,3}})?[0-9a-f]{{4,8}})?"


_FORGET_VERB = (
    r"(?:forget|delete|remove|clear|erase|drop|esque[çc]a|esquece|apague|apaga|remova)"
    r"\s+(?:(?:o|a|os|as)\s+)?"
)
_FORGET_PATTERN = re.compile(
    _LEAD
    + _FORGET_VERB
    + r"(?:"
    rf"{_MY_ANY}\s+(?:(?:personal\s+)?(?:facts|details|info|information)|dados(?:\s+pessoais)?)"
    r"|everything\s+you\s+know\s+about\s+me|tudo\s+(?:o\s+)?que\s+(?:voc[eê]|vc)\s+sabe\s+sobre\s+mim"
    r")" + _TRAIL,
    re.IGNORECASE,
)
"""Every fact at once."""

_FORGET_ONE: tuple[tuple[FactKind, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(_LEAD + _FORGET_VERB + rf"{_MY_ANY}\s+{word}" + _TRAIL, re.IGNORECASE))
    for kind, word in (
        (FactKind.EMAIL, _EMAIL_WORD),
        (FactKind.FULL_NAME, r"(?:full\s+name|nome\s+completo)"),
        (FactKind.PREFERRED_NAME, _NAME_WORD),
        (FactKind.PREFERRED_LANGUAGE, _LANGUAGE_WORD),
        (FactKind.PHONE, _PHONE_WORD),
        (FactKind.HOME_ADDRESS, _HOME_ADDRESS_WORD),
        (FactKind.BIRTH_DATE, _BIRTH_DATE_WORD),
        # A wallet may be named by its address, or by its last characters
        # (resolved against the saved ones), to forget that one of several.
        (FactKind.BTC_WALLET, _BTC_WALLET_WORD + _wallet_named(_BTC_SHAPE)),
        (FactKind.ETH_WALLET, _ETH_WALLET_WORD + _wallet_named(_ETH_SHAPE)),
    )
)
"""One kind, or one wallet. Bitcoin before Ethereum, as for setting."""

_FORGET_WALLETS = re.compile(
    _LEAD + _FORGET_VERB
    + r"(?:all\s+(?:of\s+)?|todas\s+(?:as\s+)?)?"
    + r"(?:my|minhas)\s+(?:wall?ets|carteiras|wallet\s+addresses)" + _TRAIL,
    re.IGNORECASE,
)
"""Every wallet, Ethereum and Bitcoin: "forget my wallets" left the Bitcoin
ones behind."""

_SHOW_PATTERN = re.compile(
    _LEAD
    + r"(?:"
    r"what\s+(?:do|did)\s+you\s+(?:know|remember|have)\s+(?:about|on|for)\s+me|"
    r"what\s+have\s+you\s+(?:remembered|saved|stored|got)\s+(?:about|on|for)\s+me|"
    rf"(?:do|did)\s+you\s+(?:know|have|remember|save|store)\s+my\s+"
    rf"(?:{_EMAIL_WORD}|preferred\s+name|preferred\s+language)|"
    r"(?:show|list|tell|give)(?:\s+me)?\s+my\s+(?:personal\s+)?(?:facts|details|info|information)|"
    rf"what(?:'s|\s+is)\s+my\s+(?:{_EMAIL_WORD}|preferred\s+name|preferred\s+language)|"
    r"what\s+(?:do\s+you\s+call\s+me|name\s+do\s+you\s+call\s+me|"
    r"language\s+do\s+you\s+(?:reply|answer)(?:\s+to\s+me)?\s+in)|"
    r"(?:o\s*que|oq)\s+(?:voc[eê]|vc)\s+sabe\s+sobre\s+mim|"
    r"qual\s+(?:[ée]\s+)?(?:o\s+)?meu\s+(?:e-?mail|nome\s+preferido|idioma(?:\s+preferido)?)"
    r")" + _TRAIL,
    re.IGNORECASE,
)

_WHAT_IS_MY = (
    r"(?:what(?:'s|\s+is|\s+are)\s+my|qual\s+(?:[ée]\s+)?(?:o\s+|a\s+)?(?:meu|minha)|"
    r"quais\s+(?:s[aã]o\s+)?(?:os\s+|as\s+)?(?:meus|minhas))"
)
_SHOW_ONE: tuple[tuple[FactKind, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(_LEAD + _WHAT_IS_MY + r"\s+" + word + _TRAIL, re.IGNORECASE))
    for kind, word in (
        (FactKind.PHONE, _PHONE_WORD),
        (FactKind.EMAIL, _EMAIL_WORD),
        (FactKind.FULL_NAME, r"(?:full\s+name|nome\s+completo|name|nome)"),
        (FactKind.PREFERRED_NAME, r"(?:preferred\s+name|nome\s+preferido|apelido)"),
        (FactKind.PREFERRED_LANGUAGE, _LANGUAGE_WORD),
        # Not the bare word: "what's my address?" is as often a wallet, and
        # the answer path has both in a DM prompt.
        (FactKind.HOME_ADDRESS, _HOME_ADDRESS_ASKED),
        (FactKind.BIRTH_DATE, _BIRTH_DATE_WORD),
        (FactKind.BTC_WALLET, _BTC_WALLET_WORD + r"(?:\s+address)?"),
        # Last: "wallet" alone is the Ethereum one, and "my btc wallet" also
        # ends in "wallet".
        (FactKind.ETH_WALLET, _ETH_WALLET_WORD + r"(?:\s+address)?"),
    )
)
"""One fact asked for by name. "Qual o meu telefone?" and "what's my phone
number?" went to the corpus, because only email, preferred name and language
were recognised here."""

_CAPABILITIES_PATTERN = re.compile(
    _LEAD
    + r"(?:"
    r"what\s+(?:can|do|could)\s+you\s+(?:remember|store|save|keep|know)\s+about\s+me|"
    r"what\s+(?:info(?:rmation)?|details|facts)\s+(?:can|do)\s+you\s+(?:remember|store|save|keep)"
    r"(?:\s+about\s+me)?|"
    r"(?:que|quais)\s+(?:informa[cç](?:[õo]es|ao|ão)|dados|coisas)\s+(?:voc[eê]|vc)\s+"
    r"(?:pode|consegue)\s+(?:guardar|lembrar|salvar|armazenar)(?:\s+sobre\s+mim)?|"
    r"o\s*que\s+(?:voc[eê]|vc)\s+(?:pode|consegue)\s+(?:guardar|lembrar|salvar)\s+sobre\s+mim"
    r")" + _TRAIL,
    re.IGNORECASE,
)


_SHOW_ONE_QUESTION = (
    (FactKind.HOME_ADDRESS, re.compile(
        _LEAD + r"(?:where\s+do\s+i\s+live|onde\s+(?:eu\s+)?moro)" + _TRAIL, re.IGNORECASE
    )),
    (FactKind.BIRTH_DATE, re.compile(
        _LEAD + r"(?:when\s+was\s+i\s+born|quando\s+(?:eu\s+)?nasci)" + _TRAIL, re.IGNORECASE
    )),
)


def _show_intent(text: str) -> FactIntent | None:
    for kind, pattern in (*_SHOW_ONE, *_SHOW_ONE_QUESTION):
        if pattern.match(text):
            return FactIntent(FactAction.SHOW, kind)
    if _SHOW_PATTERN.match(text):
        return FactIntent(FactAction.SHOW)
    if _CAPABILITIES_PATTERN.match(text):
        return FactIntent(FactAction.CAPABILITIES)
    return None


# The fact words a statement or request about another person is recognised by.
# Deliberately not bare "name": "João's name is on the rota" is not a fact.
_OTHERS_FACT_WORD = rf"(?:{_EMAIL_WORD}|preferred\s+name|preferred\s+language)"
_NOT_SOMEONE_ELSE = frozenset(
    {"my", "meu", "minha", "i", "me", "you", "your", "eu", "voce", "você"}
)

_ABOUT_SOMEONE_ELSE_PATTERNS = tuple(
    re.compile(_LEAD + body + _TRAIL, re.IGNORECASE)
    for body in (
        rf"(?P<who>[^\s']+(?:\s+[^\s']+){{0,2}})(?:'s|s')\s+{_OTHERS_FACT_WORD}\s+is\s+.+?",
        rf"(?:his|her|their)\s+{_OTHERS_FACT_WORD}\s+is\s+.+?",
        rf"(?:set|change|update)\s+(?P<who>\S+(?:\s+\S+)?)(?:'s|s')\s+{_OTHERS_FACT_WORD}\s+to\s+.+?",
        r"call\s+(?:him|her|them|<@!?\d+>)\s+.+?",
        r"(?:o\s+)?(?:e-?mail|nome\s+preferido|idioma)\s+d[aoe]\s+(?P<who>\S+(?:\s+\S+)?)\s+[ée]\s+.+?",
    )
)

# Requests for somebody else's facts are matched anywhere in the message: one
# buried in a longer question is refused as firmly as one on its own.
_OTHERS_FACTS_PATTERNS = tuple(
    re.compile(body, re.IGNORECASE)
    for body in (
        r"\b(?:what(?:'s|\s+is|\s+are)|tell\s+me|give\s+me|send\s+me|share|show\s+me|find|"
        r"get\s+me|do\s+you\s+(?:know|have))\s+(?:the\s+)?"
        rf"(?P<who>[^\s?']+(?:\s+[^\s?']+)?)(?:'s|s')\s+{_OTHERS_FACT_WORD}",
        rf"\b(?:what|which)\s+{_OTHERS_FACT_WORD}\s+(?:does|do|did|is)\s+(?P<who>\S+)",
        rf"\b(?:does|do|has|have|did)\s+(?P<who>\S+(?:\s+\S+)?)\s+(?:have|has|set|give|given|"
        rf"tell\s+you|told\s+you)\s+(?:you\s+)?(?:an?\s+|their\s+|his\s+|her\s+)?{_OTHERS_FACT_WORD}",
        rf"{_OTHERS_FACT_WORD}\s+(?:of|for|from)\s+<@!?\d+>",
        r"<@!?\d+>(?:'s|s')?\s+(?:e-?mail|preferred\s+name|preferred\s+language)",
        r"\bqual\s+(?:[ée]\s+)?(?:o\s+)?(?:e-?mail|nome\s+preferido|idioma)\s+d[aoe]\s+(?P<who>\S+)",
    )
)

_UNSUPPORTED_PATTERNS = tuple(
    re.compile(body, re.IGNORECASE)
    for body in (
        # "remember that I ..." -- a request to keep something about oneself.
        rf"^(?:{_GREETING}\b[,!.]?\s*)*(?:{_POLITE}\b,?\s*)*{_REMEMBER}\s+"
        r"(?:that\s+|que\s+|de\s+que\s+)?(?:this\b|isso\b|the\s+following\b|"
        r"(?:i|i'm|i've|my|me|mine|eu|meu|minha)\b)",
        # "my phone number is ..." -- a personal attribute outside the set.
        _LEAD
        + r"(?:my\s+(?:phone(?:\s+number)?|mobile(?:\s+number)?|cell(?:\s+number)?|number|"
        r"pronouns|time\s*zone|"
        r"age|location|city|country|password|job\s+title|github|twitter|linkedin|telegram|"
        r"whatsapp|surname|last\s+name|favou?rite\s+\w+)\s+(?:is|are)|"
        r"meu\s+(?:telefone|celular|n[uú]mero|fuso(?:\s+hor[aá]rio)?|"
        r"cargo|sobrenome)\s+(?:[ée]|s[aã]o))\s+.+",
    )
)


def _collapse_text(text: str) -> str:
    # Curly apostrophes are what phones type; "João’s email" is still a
    # possessive.
    return " ".join(text.replace("’", "'").split())


def _someone_else(match: re.Match[str]) -> bool:
    who = match.groupdict().get("who")
    if who is None:
        return True
    return who.split()[-1].lower() not in _NOT_SOMEONE_ELSE


def _clean_value(value: str) -> str:
    return value.strip().strip(_QUOTES).strip()


def _plausible(kind: FactKind, value: str) -> bool:
    words = value.split()
    if not words:
        return False
    if kind is FactKind.PREFERRED_NAME:
        return len(words) <= _MAX_NAME_WORDS and words[0].lower() not in _NOT_A_NAME_START
    if kind is FactKind.FULL_NAME:
        return len(words) <= _MAX_FULL_NAME_WORDS and words[0].lower() not in _NOT_A_NAME_START
    if kind is FactKind.PREFERRED_LANGUAGE:
        return len(words) <= _MAX_LANGUAGE_WORDS
    if kind is FactKind.HOME_ADDRESS:
        # "my address is 0x..." is a wallet said loosely, not where they live.
        return len(words) <= _MAX_ADDRESS_WORDS and not _HEX_ADDRESS.match(value)
    return True


_HEX_ADDRESS = re.compile(r"0x[0-9a-f]{6}", re.IGNORECASE)

# Where a name stops. A comma, a semicolon or a sentence-ending period (not the
# one in "A. Santos"), and a connector: "Leo, tenho 45 anos" and "Leo e sou
# dev" are the name Leo and something else. A full name keeps " e " before a
# capitalised word, because "Araujo e Silva" is one surname.
_NAME_END = re.compile(r"[,;!?]|(?<=\w\w)\.(?=\s|$)")
_NAME_CONNECTOR = re.compile(r"\s(?:e|and)\s", re.IGNORECASE)
_FULL_NAME_CONNECTOR = re.compile(r"\s(?:e|and)\s(?=[a-zà-ÿ])")

# The part of a value that has the kind's shape, when the rest of the clause
# is something else: "+5521980703795 morro no Rio" was refused whole as a
# phone number that is too long.
_SHAPED_PREFIX = {
    FactKind.PHONE: re.compile(r"\+?\(?\d[\d\s\-().]*\d"),
    FactKind.BIRTH_DATE: re.compile(
        r"\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{4}-\d{1,2}-\d{1,2}|"
        r"\d{1,2}(?:st|nd|rd|th|º)?\s+(?:de\s+|of\s+)?[a-zç]+\.?,?\s+(?:de\s+)?\d{4}|"
        r"[a-zç]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}",
        re.IGNORECASE,
    ),
}


def _cut(kind: FactKind, value: str) -> str:
    """The value alone, without what the person went on to say after it."""
    if kind in (FactKind.PREFERRED_NAME, FactKind.FULL_NAME):
        value = _NAME_END.split(value, maxsplit=1)[0]
        connector = _FULL_NAME_CONNECTOR if kind is FactKind.FULL_NAME else _NAME_CONNECTOR
        return connector.split(value, maxsplit=1)[0].strip()
    shaped = _SHAPED_PREFIX.get(kind)
    found = shaped.match(value) if shaped is not None else None
    return found.group(0) if found is not None else value


def _forget_intent(text: str) -> FactIntent | None:
    if _FORGET_PATTERN.match(text):
        return FactIntent(FactAction.FORGET)
    if _FORGET_WALLETS.match(text):
        return FactIntent(FactAction.FORGET, FactKind.ETH_WALLET, all_wallets=True)
    for kind, pattern in _FORGET_ONE:
        match = pattern.match(text)
        if match is not None:
            value = match.groupdict().get("value")
            return FactIntent(FactAction.FORGET, kind, _clean_value(value) if value else None)
    return None


_MY_NAME_IS = re.compile(
    _LEAD + r"(?:my\s+name\s+is|meu\s+nome\s+[ée]|(?:eu\s+)?me\s+chamo)\s+(?P<value>.+?)"
    + _TRAIL,
    re.IGNORECASE,
)
""""My name is X": a full name when X is several words, the name to call them
by when it is one -- which is what it always meant before full names existed."""


def _set_intent(text: str) -> FactIntent | None:
    named = _MY_NAME_IS.match(text)
    if named is not None:
        value = _cut(FactKind.FULL_NAME, _clean_value(named.group("value")))
        kind = FactKind.FULL_NAME if len(value.split()) > 1 else FactKind.PREFERRED_NAME
        if _plausible(kind, value):
            return FactIntent(FactAction.SET, kind, value)
    for kind, pattern in _SET_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        value = _cut(kind, _clean_value(match.group("value")))
        if _plausible(kind, value):
            return FactIntent(FactAction.SET, kind, value)
    return None


_USER_MENTION = re.compile(r"<@[!&]?\d+>[,:;]?")


_CONTACT_KINDS = frozenset(
    {FactKind.EMAIL, FactKind.PHONE, FactKind.HOME_ADDRESS, FactKind.BIRTH_DATE}
)
"""What a channel message must not be archived for stating: the ways of
reaching a person, where they live, and when they were born."""


def states_own_contact(text: str) -> bool:
    """Whether a channel message is someone giving their own email, phone,
    home address or birth date.

    Alone or within an introduction: "my name is ..., my phone is ..." was once
    archived whole, because only a message that was *entirely* an email
    statement was recognised.

    For ingest, not for asks: such a message must never enter the corpus. The
    assistant stores the address and promises to show it only in its owner's
    DMs; a stored copy of the message would hand it to anyone who can read the
    channel, through retrieval and citations, in the room or in their own DMs.

    Every mention is removed, not only the assistant's, because ingest does not
    know which account is the assistant -- and "@someone my email is ..." is
    withheld just the same, which errs on the private side. It does not require
    the message to be addressed to the assistant for the same reason. A
    malformed address still counts: an almost-email is still personal data.
    """
    cleaned = _collapse_text(_USER_MENTION.sub(" ", text))
    intent = fact_intent(cleaned)
    if intent is not None and intent.action is FactAction.SET:
        return intent.kind in _CONTACT_KINDS
    if intent is not None and intent.action is FactAction.SET_MANY:
        return any(kind in _CONTACT_KINDS for kind, _ in intent.sets)
    # Any clause, whatever the rest of the message is: "call me when it's
    # done, my email is ..." is not an introduction, and still gives an email.
    return any(
        (found := _set_intent(clause)) is not None and found.kind in _CONTACT_KINDS
        for clause in _clauses(cleaned)
    )


_CLAUSE_START = re.compile(
    r"\b(?:my|meu|minha|call\s+me|(?:you\s+can\s+)?call\s+me|pode\s+me\s+chamar|"
    r"(?:eu\s+)?me\s+chamo|me\s+cham[ae]|"
    # "morro" (a typo of "moro") only where a clause can start: after "o" or
    # "do" it is the hill in "subi o morro no domingo".
    r"(?:eu\s+)?moro|(?<!\b[oa]\s)(?<!\b[dn][oa]\s)(?<!\bum\s)morro(?=\s+(?:n[oa]s?|em)\b)|"
    r"resido|i\s+live|"
    r"sou\s+de|i'?m\s+from|i\s+am\s+from|"
    r"(?:eu\s+)?tenho(?=\s+\d)|i'?m(?=\s+\d)|i\s+am(?=\s+\d)|"
    r"(?:eu\s+)?nasci|nascid[oa]|i\s+was\s+born|born\s+on)\b",
    re.IGNORECASE,
)
"""Where a fact starts in a longer message. A clause runs to the next one, so
every fact a message states needs a starter here -- "tenho 45 anos nasci em
..." once ran the preferred name on to the end of the birth date."""
_CLAUSE_TAIL = re.compile(r"[\s,;.!]*(?:\b(?:e|and)\b)?[\s,;.!]*$", re.IGNORECASE)

AGE = "age"
WHERE_YOURE_FROM = "where you're from"
_NOT_KEPT = (
    # An age goes stale on the next birthday, and follows from the birth date.
    # English "I'm 45" only as the whole clause or before "years"/"and": "I'm
    # 100% sure" and "I'm 5 minutes away" are not ages.
    (AGE, re.compile(
        r"^(?:(?:eu\s+)?tenho\s+\d{1,3}\s+anos\b|"
        r"(?:i'?m|i\s+am)\s+\d{1,3}(?:$|\s+(?:years?|y/?o|and)\b))",
        re.IGNORECASE,
    )),
    # Where somebody is from, or was born, is not where they live.
    (WHERE_YOURE_FROM, re.compile(
        r"^(?:sou\s+de|i'?m\s+from|i\s+am\s+from|(?:eu\s+)?nasci\s+(?:em|n[oa]s?)|"
        r"i\s+was\s+born\s+in)\b",
        re.IGNORECASE,
    )),
)


def _clauses(text: str) -> list[str]:
    """The message cut where each fact starts, connectors trimmed.

    Whatever precedes the first fact ("Oi", "Hello there") is dropped.
    """
    # Matches never overlap, so "you can call me" starts once, not twice, and
    # a short clause ("I'm 45, my email is ...") keeps the one after it.
    starts = [m.start() for m in _CLAUSE_START.finditer(text)]
    if not starts:
        return []
    bounds = zip(starts, [*starts[1:], len(text)], strict=True)
    return [_CLAUSE_TAIL.sub("", text[a:b]).strip() for a, b in bounds]


def _introduction(text: str) -> FactIntent | None:
    """Several facts stated in one message, or None.

    Two or more parts must be recognised, so a single fact keeps its own
    path. Every part stated is accounted for in the reply: saved, or named as
    not kept.
    """
    sets: list[tuple[FactKind, str]] = []
    not_kept: list[str] = []
    for clause in _clauses(text):
        found = _set_intent(clause)
        if found is not None and found.kind is not None and found.value is not None:
            sets.append((found.kind, found.value))
            continue
        not_kept += [note for note, pattern in _NOT_KEPT if pattern.match(clause)]
    if not sets or len(sets) + len(not_kept) < 2:
        return None
    return FactIntent(
        FactAction.SET_MANY, sets=tuple(sets), not_kept=tuple(dict.fromkeys(not_kept))
    )


def fact_intent(text: str) -> FactIntent | None:
    """What this message asks of the asker's personal facts, or None.

    Only ever called on the asker's own message, and never on retrieved
    content, remembered turns or tool output: those are data, and a fact set
    from data is a fact somebody else planted. None means an ordinary question.

    The order is the safety order. Requests about somebody else are recognised
    before anything that could store, so "remember João's email is ..." is
    refused rather than read as a statement by the speaker.
    """
    collapsed = _collapse_text(text)
    if not collapsed:
        return None
    if any(
        (m := p.match(collapsed)) is not None and _someone_else(m)
        for p in _ABOUT_SOMEONE_ELSE_PATTERNS
    ):
        return FactIntent(FactAction.ABOUT_SOMEONE_ELSE)
    if any(
        (m := p.search(collapsed)) is not None and _someone_else(m)
        for p in _OTHERS_FACTS_PATTERNS
    ):
        return FactIntent(FactAction.OTHERS_FACTS)
    intent = _forget_intent(collapsed) or _introduction(collapsed) or _set_intent(collapsed)
    if intent is not None:
        return intent
    shown = _show_intent(collapsed)
    if shown is not None:
        return shown
    if any(p.search(collapsed) for p in _UNSUPPORTED_PATTERNS):
        return FactIntent(FactAction.UNSUPPORTED)
    return None
