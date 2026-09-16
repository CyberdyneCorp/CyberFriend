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
