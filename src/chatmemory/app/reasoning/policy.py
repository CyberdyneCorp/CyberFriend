"""The corrective policy: a pure function, then a table of conditions.

Three separate things live here and the separation is the design:

*   `action_for` maps a verdict to an action. It is total, pure, and takes no
    context -- the same verdict always yields the same action, and the model
    never participates in the choice. Every test of it runs without a model.
*   Conditions decide whether an action is *executable* right now. Their
    vocabulary is a closed enum rather than an expression language, so a
    configuration validates when it loads instead of failing mid-request.
*   Permission is decided by a separate collaborator the configuration cannot
    name or reach. There is no `Condition` member meaning "permitted", so
    "edit the config to allow X" is not a sentence this language can express.
    The two predicates combine conjunctively; the invariant is their
    separation, not the order they run in.

Note what is absent: the budget. A policy may ask for another attempt
forever; the driver is what stops. And note what `apply_action` cannot touch:
`SearchQuery` has no permission field, so widening a search has no channel
set to widen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol, TypeVar

from chatmemory.app.reasoning.errors import ConfigurationError
from chatmemory.app.reasoning.verdicts import Verdict
from chatmemory.domain.search import SearchQuery

_EnumT = TypeVar("_EnumT", bound=StrEnum)

MAX_RESULT_LIMIT = 50
"""Ceiling on widening, matching the retrieval surface's own cap."""


class Action(StrEnum):
    """Everything a run may do next. A closed set, like the verdicts."""

    ANSWER = "answer"
    ABSTAIN = "abstain"
    REFORMULATE = "reformulate"
    WIDEN_RESULTS = "widen_results"
    WIDEN_TIME_RANGE = "widen_time_range"


class Condition(StrEnum):
    """The closed vocabulary configuration may draw on.

    Deliberately contains no member meaning "permitted", "allowed" or
    "authorised": permission is not expressible here at all.
    """

    REQUIRES_EVIDENCE = "requires_evidence"
    REQUIRES_REMAINING_ATTEMPTS = "requires_remaining_attempts"
    REQUIRES_TIME_RANGE = "requires_time_range"
    REQUIRES_RESULT_HEADROOM = "requires_result_headroom"
    REQUIRES_OPERATOR_OPT_IN = "requires_operator_opt_in"


INHERENT_CONDITIONS: Mapping[Action, frozenset[Condition]] = {
    Action.ANSWER: frozenset({Condition.REQUIRES_EVIDENCE}),
    # Abstention is unconditional on purpose: it is the action that is always
    # available, which is what makes "every reachable state has something it
    # may do" a property that can be checked at load time.
    Action.ABSTAIN: frozenset(),
    Action.REFORMULATE: frozenset({Condition.REQUIRES_REMAINING_ATTEMPTS}),
    Action.WIDEN_RESULTS: frozenset(
        {Condition.REQUIRES_REMAINING_ATTEMPTS, Condition.REQUIRES_RESULT_HEADROOM}
    ),
    Action.WIDEN_TIME_RANGE: frozenset(
        {Condition.REQUIRES_REMAINING_ATTEMPTS, Condition.REQUIRES_TIME_RANGE}
    ),
}

_VERDICT_ACTIONS: Mapping[Verdict, Action] = {
    Verdict.SUFFICIENT: Action.ANSWER,
    # Partial coverage is the case a naive gate mistakes for complete: ask for
    # more of the same rather than changing the question.
    Verdict.PARTIAL: Action.WIDEN_RESULTS,
    Verdict.IRRELEVANT: Action.REFORMULATE,
    # Nothing came back at all, so the query matched no window: reach further
    # back in time before assuming the words were wrong.
    Verdict.EMPTY: Action.WIDEN_TIME_RANGE,
    Verdict.AMBIGUOUS: Action.REFORMULATE,
    Verdict.UNANSWERABLE: Action.ABSTAIN,
}


def action_for(verdict: Verdict) -> Action:
    """Map a verdict to the corrective action. Pure, total, model-free."""
    return _VERDICT_ACTIONS[verdict]


class BlockedReason(StrEnum):
    """Why an action was not taken -- the operator's handle on a dead end.

    A configured condition is reported separately from an inherent one so a
    terminal outcome can tell a policy problem from a corpus problem.
    """

    UNMET_CONDITION = "unmet_condition"
    CONFIGURED_CONDITION = "configured_condition"
    NOT_PERMITTED = "not_permitted"


@dataclass(frozen=True, slots=True)
class BlockedAction:
    action: Action
    reason: BlockedReason
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Everything the conditions are evaluated against.

    `attempts_remaining` is reported by the driver and can only *block* an
    action here. The driver re-checks its own budget regardless of what this
    says, so a context that lied would buy a policy nothing.
    """

    evidence_count: int = 0
    attempts_remaining: int = 0
    has_time_range: bool = False
    result_headroom: bool = False
    operator_opt_in: frozenset[Action] = frozenset()


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: Action
    blocked: tuple[BlockedAction, ...] = ()

    @property
    def blocked_by_configuration(self) -> bool:
        """Whether a configured -- as opposed to inherent -- condition is what
        refused a preferred action. This is the signal that separates a policy
        problem from a corpus problem in the operator record."""
        return any(b.reason is BlockedReason.CONFIGURED_CONDITION for b in self.blocked)


class ActionAuthorizer(Protocol):
    """Permission, as a collaborator configuration cannot reach.

    It is passed to the policy by the composition root, is not a field on
    `PolicyConfig`, and is not nameable from the condition vocabulary.
    """

    def may(self, action: Action) -> bool: ...


class ReadOnlyCorrectiveActions:
    """The default authorizer.

    Every corrective action here rewrites a query and reads again, so all of
    them are permitted. That it currently denies nothing is not the point:
    the point is that the denial mechanism exists outside configuration's
    reach, so a future denial cannot be configured away.
    """

    def may(self, action: Action) -> bool:
        return action in INHERENT_CONDITIONS


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Deployment-editable restrictions. Restrictions only.

    Conditions attached here *union* with an action's inherent ones. There is
    no representation for removing an inherent condition, and none for
    granting permission.
    """

    added_conditions: Mapping[Action, frozenset[Condition]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Sequence[str]]) -> PolicyConfig:
        """Parse and validate at load. Unknown words are a boot failure."""
        parsed: dict[Action, frozenset[Condition]] = {}
        for action_name, condition_names in raw.items():
            action = _parse(Action, action_name, "action")
            parsed[action] = frozenset(
                _parse(Condition, name, "condition") for name in condition_names
            )
        return cls(added_conditions=parsed)

    def conditions_for(self, action: Action) -> frozenset[Condition]:
        """Inherent conditions, plus whatever configuration added. Never fewer."""
        return INHERENT_CONDITIONS[action] | self.added_conditions.get(action, frozenset())


def _parse(enum: type[_EnumT], value: str, kind: str) -> _EnumT:
    try:
        return enum(value)
    except ValueError as exc:
        allowed = ", ".join(sorted(m.value for m in enum))
        raise ConfigurationError(
            f"unknown {kind} {value!r}; the vocabulary is closed and allows: {allowed}"
        ) from exc


def _satisfied(condition: Condition, action: Action, context: ActionContext) -> bool:
    if condition is Condition.REQUIRES_EVIDENCE:
        return context.evidence_count > 0
    if condition is Condition.REQUIRES_REMAINING_ATTEMPTS:
        return context.attempts_remaining > 0
    if condition is Condition.REQUIRES_TIME_RANGE:
        return context.has_time_range
    if condition is Condition.REQUIRES_RESULT_HEADROOM:
        return context.result_headroom
    return action in context.operator_opt_in


class CorrectivePolicy:
    """Chooses the next action: a pure mapping, filtered by availability.

    The fallback order is fixed and ends at abstention, which no
    configuration may condition (that is rejected at load).
    """

    def __init__(
        self,
        config: PolicyConfig | None = None,
        authorizer: ActionAuthorizer | None = None,
    ) -> None:
        self._config = config or PolicyConfig()
        # Separate object, separate source. The config above cannot name it.
        self._authorizer = authorizer or ReadOnlyCorrectiveActions()
        validate_config(self._config)

    @property
    def config(self) -> PolicyConfig:
        return self._config

    def decide(self, verdict: Verdict, context: ActionContext) -> PolicyDecision:
        blocked: list[BlockedAction] = []
        for action in self._candidates(verdict):
            refusal = self._refusal(action, context)
            if refusal is None:
                return PolicyDecision(action, tuple(blocked))
            blocked.append(refusal)
        # Unreachable while abstention stays unconditional, which load-time
        # validation guarantees; kept so a regression fails loudly.
        raise ConfigurationError("no action available; configuration conditioned abstention")

    def _candidates(self, verdict: Verdict) -> tuple[Action, ...]:
        preferred = action_for(verdict)
        fallbacks = (Action.REFORMULATE, Action.ANSWER, Action.ABSTAIN)
        return (preferred, *(a for a in fallbacks if a is not preferred))

    def _refusal(self, action: Action, context: ActionContext) -> BlockedAction | None:
        # Permission first or conditions first makes no difference to the
        # outcome: both must hold. The invariant is that they come from two
        # objects neither of which can edit the other, not their order.
        if not self._authorizer.may(action):
            return BlockedAction(action, BlockedReason.NOT_PERMITTED)
        unmet = sorted(
            c
            for c in self._config.conditions_for(action)
            if not _satisfied(c, action, context)
        )
        if not unmet:
            return None
        added = self._config.added_conditions.get(action, frozenset())
        reason = (
            BlockedReason.CONFIGURED_CONDITION
            if any(c in added for c in unmet)
            else BlockedReason.UNMET_CONDITION
        )
        return BlockedAction(action, reason, ", ".join(str(c) for c in unmet))


def validate_config(config: PolicyConfig) -> None:
    """Reject a configuration that could strand a run.

    Every state a run can reach is a verdict. From each of them at least one
    candidate action must require no conditions at all -- otherwise a run
    could arrive somewhere with nothing it may do, and would discover it
    mid-request rather than at boot.
    """
    for verdict in Verdict:
        candidates = (action_for(verdict), Action.REFORMULATE, Action.ANSWER, Action.ABSTAIN)
        if not any(not config.conditions_for(a) for a in candidates):
            raise ConfigurationError(
                f"no unconditional action remains for verdict {verdict!r}; "
                "configuration may restrict actions but must leave a way out"
            )


def widen_time_range(query: SearchQuery) -> SearchQuery:
    """Reach further back, leaving every other facet alone."""
    if query.since is None:
        return query
    if query.until is None:
        return replace(query, since=None)
    # Double the span backwards rather than dropping the bound entirely: an
    # unbounded scan is a different query, not a wider one.
    return replace(query, since=query.since - (query.until - query.since))


def deterministic_reformulation(text: str) -> str:
    """A reformulation that needs no model.

    Drops the interrogative frame so the remaining terms carry the lexical
    search. Used when the critic suggests nothing, which keeps the corrective
    path runnable -- and testable -- with no model at all.
    """
    dropped = {
        "what", "who", "when", "where", "why", "how", "did", "does", "do",
        "is", "are", "was", "were", "the", "a", "an", "of", "to", "in", "on",
        "any", "anyone", "someone", "please", "tell", "me", "about",
    }
    kept = [w for w in text.replace("?", " ").split() if w.lower() not in dropped]
    return " ".join(kept) if kept else text


def apply_action(
    query: SearchQuery, action: Action, suggestion: str | None = None
) -> SearchQuery:
    """Rewrite a query for a corrective action.

    Takes no viewer and no channel set, and returns a `SearchQuery`, which
    has no permission field. Widening the query, the result count or the time
    range is therefore all it *can* do -- broadening the channels searched is
    not something this function could express if it tried.
    """
    if action is Action.REFORMULATE:
        return replace(query, text=suggestion or deterministic_reformulation(query.text))
    if action is Action.WIDEN_RESULTS:
        return replace(query, limit=min(query.limit * 2, MAX_RESULT_LIMIT))
    if action is Action.WIDEN_TIME_RANGE:
        return widen_time_range(query)
    return query


TERMINAL_ACTIONS = frozenset({Action.ANSWER, Action.ABSTAIN})
