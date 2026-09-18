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
has already been declined.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

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
_PHONE_WORD = (
    r"(?:phone(?:\s+number)?|mobile|cell(?:\s*phone)?|whatsapp|"
    r"telefone|celular|n[uú]mero(?:\s+de\s+telefone)?)"
)
# "wallet" alone means the Ethereum one: it is the chain this deployment can
# read balances on, and somebody who says "my wallet is 0x..." means that.
_ETH_WALLET_WORD = (
    r"(?:(?:eth(?:ereum)?|evm|base)\s+(?:wallet|address|endere[cç]o)|"
    r"wallet|carteira)"
)
_BTC_WALLET_WORD = (
    r"(?:(?:btc|bitcoin)\s+(?:wallet|address|endere[cç]o)|"
    r"carteira\s+(?:btc|bitcoin))"
)

_SET_PATTERNS: tuple[tuple[FactKind, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(_LEAD + body + _TRAIL, re.IGNORECASE))
    for kind, body in (
        (
            FactKind.PREFERRED_NAME,
            r"(?:(?:you\s+can\s+|just\s+)?call\s+me|my\s+(?:preferred\s+)?name\s+is|"
            r"i\s+(?:prefer|like|want)\s+to\s+be\s+called|"
            r"(?:set|change|update)\s+my\s+(?:preferred\s+)?name\s+to|"
            r"(?:me\s+chame|me\s+chama|me\s+chamem|pode\s+me\s+chamar)\s+de|"
            r"meu\s+nome\s+(?:preferido\s+)?[ée]|prefiro\s+ser\s+chamad[oa]\s+de)"
            r"\s+(?P<value>.+?)",
        ),
        (
            FactKind.EMAIL,
            rf"(?:my\s+{_EMAIL_WORD}\s+is|(?:set|change|update)\s+my\s+{_EMAIL_WORD}\s+to|"
            r"meu\s+e-?mail\s+[ée]|(?:mude|muda|altere|atualize)\s+(?:o\s+)?meu\s+e-?mail\s+para)"
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
            rf"(?:{_MY}\s+{_PHONE_WORD}\s+{_IS}|"
            rf"(?:set|change|update)\s+my\s+{_PHONE_WORD}\s+to|"
            rf"(?:mude|muda|altere|atualize)\s+(?:o\s+)?meu\s+{_PHONE_WORD}\s+para)"
            r"\s+(?P<value>.+?)",
        ),
        # Bitcoin before Ethereum: "my btc wallet is ..." also matches the
        # Ethereum pattern's bare "wallet", and the first match wins.
        (
            FactKind.BTC_WALLET,
            rf"(?:{_MY}\s+{_BTC_WALLET_WORD}\s+{_IS}|"
            rf"(?:set|change|update)\s+my\s+{_BTC_WALLET_WORD}\s+to)"
            r"\s+(?P<value>\S+?)",
        ),
        (
            FactKind.ETH_WALLET,
            rf"(?:{_MY}\s+{_ETH_WALLET_WORD}\s+{_IS}|"
            rf"(?:set|change|update)\s+my\s+{_ETH_WALLET_WORD}\s+to|"
            rf"(?:mude|muda|altere|atualize)\s+(?:a\s+)?minha\s+carteira\s+para)"
            r"\s+(?P<value>\S+?)",
        ),
    )
)

# A name someone asks to be called is a few words. The bound and the stop
# words are what keep "call me when the deploy is done" out of the name field:
# a miss here is answered as an ordinary question, which is recoverable.
_MAX_NAME_WORDS = 4
_MAX_LANGUAGE_WORDS = 3
_NOT_A_NAME_START = frozenset({
    "when", "if", "after", "before", "back", "later", "tomorrow", "today",
    "tonight", "at", "on", "in", "about", "once", "whenever", "as", "by", "and",
    "or", "to", "anytime", "maybe", "asap", "now", "sometime", "quando", "se",
    "depois", "amanhã", "amanha", "mais", "hoje", "agora",
})
_QUOTES = "\"'`“”‘’"

_FORGET_PATTERN = re.compile(
    _LEAD
    + r"(?:forget|delete|remove|clear|erase|drop|esque[çc]a|esquece|apague|apaga|remova)"
    r"\s+(?:(?:o|a|os|as)\s+)?(?:"
    rf"{_MY}\s+(?:(?P<email>{_EMAIL_WORD})|(?P<name>{_NAME_WORD})|(?P<language>{_LANGUAGE_WORD})|"
    r"(?:personal\s+)?(?:facts|details|info|information)|dados(?:\s+pessoais)?)"
    r"|everything\s+you\s+know\s+about\s+me|tudo\s+(?:o\s+)?que\s+(?:voc[eê]|vc)\s+sabe\s+sobre\s+mim"
    r")" + _TRAIL,
    re.IGNORECASE,
)

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
    r"o\s+que\s+(?:voc[eê]|vc)\s+sabe\s+sobre\s+mim|"
    r"qual\s+(?:[ée]\s+)?(?:o\s+)?meu\s+(?:e-?mail|nome\s+preferido|idioma(?:\s+preferido)?)"
    r")" + _TRAIL,
    re.IGNORECASE,
)

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
        r"birthday|birth\s*date|date\s+of\s+birth|(?:home\s+)?address|pronouns|time\s*zone|"
        r"age|location|city|country|password|job\s+title|github|twitter|linkedin|telegram|"
        r"whatsapp|surname|last\s+name|full\s+name|favou?rite\s+\w+)\s+(?:is|are)|"
        r"meu\s+(?:telefone|celular|n[uú]mero|anivers[aá]rio|endere[çc]o|fuso(?:\s+hor[aá]rio)?|"
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
    if kind is FactKind.PREFERRED_LANGUAGE:
        return len(words) <= _MAX_LANGUAGE_WORDS
    return True


def _forget_intent(text: str) -> FactIntent | None:
    match = _FORGET_PATTERN.match(text)
    if match is None:
        return None
    for group, kind in (
        ("email", FactKind.EMAIL),
        ("name", FactKind.PREFERRED_NAME),
        ("language", FactKind.PREFERRED_LANGUAGE),
    ):
        if match.group(group):
            return FactIntent(FactAction.FORGET, kind)
    return FactIntent(FactAction.FORGET)


def _set_intent(text: str) -> FactIntent | None:
    for kind, pattern in _SET_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        value = _clean_value(match.group("value"))
        if _plausible(kind, value):
            return FactIntent(FactAction.SET, kind, value)
    return None


_USER_MENTION = re.compile(r"<@[!&]?\d+>[,:;]?")


def states_own_email(text: str) -> bool:
    """Whether a channel message is someone giving their own email address.

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
    intent = fact_intent(_USER_MENTION.sub(" ", text))
    return (
        intent is not None
        and intent.action is FactAction.SET
        and intent.kind is FactKind.EMAIL
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
    intent = _forget_intent(collapsed) or _set_intent(collapsed)
    if intent is not None:
        return intent
    if _SHOW_PATTERN.match(collapsed):
        return FactIntent(FactAction.SHOW)
    if any(p.search(collapsed) for p in _UNSUPPORTED_PATTERNS):
        return FactIntent(FactAction.UNSUPPORTED)
    return None
