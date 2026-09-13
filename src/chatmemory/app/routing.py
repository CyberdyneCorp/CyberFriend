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
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
