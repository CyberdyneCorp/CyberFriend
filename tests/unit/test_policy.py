"""The corrective policy, exhaustively, with no model in the room.

That these tests need no model is the payoff of the enumerated verdict. A
critic emitting prose would leave nothing here to assert.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from chatmemory.app.reasoning.errors import ConfigurationError
from chatmemory.app.reasoning.policy import (
    MAX_RESULT_LIMIT,
    Action,
    ActionContext,
    BlockedReason,
    Condition,
    CorrectivePolicy,
    PolicyConfig,
    action_for,
    apply_action,
    deterministic_reformulation,
    validate_config,
)
from chatmemory.app.reasoning.verdicts import Verdict
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.search import SearchQuery

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def query(**kwargs: object) -> SearchQuery:
    base: dict[str, object] = {"text": "who owns the deploy script", "limit": 20}
    base.update(kwargs)
    return SearchQuery(**base)  # type: ignore[arg-type]


def permissive() -> ActionContext:
    return ActionContext(
        evidence_count=3, attempts_remaining=2, has_time_range=True, result_headroom=True
    )


# --- the pure function -------------------------------------------------


@pytest.mark.parametrize("verdict", list(Verdict))
def test_every_verdict_maps_to_an_action(verdict: Verdict) -> None:
    """Total over the enum: a new verdict cannot be added without a decision."""
    assert action_for(verdict) in set(Action)


@pytest.mark.parametrize("verdict", list(Verdict))
def test_the_same_verdict_always_yields_the_same_action(verdict: Verdict) -> None:
    assert len({action_for(verdict) for _ in range(5)}) == 1


def test_the_mapping_is_the_one_specified() -> None:
    assert action_for(Verdict.SUFFICIENT) is Action.ANSWER
    assert action_for(Verdict.PARTIAL) is Action.WIDEN_RESULTS
    assert action_for(Verdict.IRRELEVANT) is Action.REFORMULATE
    assert action_for(Verdict.EMPTY) is Action.WIDEN_TIME_RANGE
    assert action_for(Verdict.AMBIGUOUS) is Action.REFORMULATE
    assert action_for(Verdict.UNANSWERABLE) is Action.ABSTAIN


# --- widening cannot reach the channel set -----------------------------


@pytest.mark.parametrize("action", list(Action))
def test_no_action_changes_the_channel_or_author_set(action: Action) -> None:
    """The structural guarantee, asserted behaviourally as well.

    `SearchQuery` has no permission field, so widening has no channel set to
    widen; `channels` here is the asker's own stated preference, and
    narrowing or preserving it can never grant access.
    """
    original = query(
        channels=frozenset({ChannelRef("discord", 100)}),
        authors=frozenset({PersonRef("discord", 7)}),
        since=NOW - timedelta(days=1),
        until=NOW,
    )
    widened = apply_action(original, action, suggestion="something else")
    assert widened.channels == original.channels
    assert widened.authors == original.authors


def test_apply_action_cannot_be_given_a_viewer() -> None:
    """Not merely untested: there is no parameter to pass one through."""
    import inspect

    params = set(inspect.signature(apply_action).parameters)
    assert params == {"query", "action", "suggestion"}


def test_widening_results_doubles_up_to_the_cap() -> None:
    assert apply_action(query(limit=20), Action.WIDEN_RESULTS).limit == 40
    assert apply_action(query(limit=40), Action.WIDEN_RESULTS).limit == MAX_RESULT_LIMIT


def test_widening_time_doubles_the_span_backwards() -> None:
    original = query(since=NOW - timedelta(days=2), until=NOW)
    widened = apply_action(original, Action.WIDEN_TIME_RANGE)
    assert widened.since == NOW - timedelta(days=4)
    assert widened.until == NOW


def test_widening_time_without_an_upper_bound_drops_the_lower_one() -> None:
    widened = apply_action(query(since=NOW - timedelta(days=2)), Action.WIDEN_TIME_RANGE)
    assert widened.since is None


def test_reformulation_prefers_a_suggestion_and_falls_back_deterministically() -> None:
    assert apply_action(query(), Action.REFORMULATE, "deploy ownership").text == (
        "deploy ownership"
    )
    assert apply_action(query(), Action.REFORMULATE).text == (
        deterministic_reformulation("who owns the deploy script")
    )
    assert deterministic_reformulation("who owns the deploy script") == "owns deploy script"


def test_terminal_actions_leave_the_query_untouched() -> None:
    original = query()
    assert apply_action(original, Action.ANSWER) == original
    assert apply_action(original, Action.ABSTAIN) == original


# --- configuration restricts, never authorises -------------------------


def test_configuration_conditions_union_with_inherent_ones() -> None:
    config = PolicyConfig.from_mapping({"widen_results": ["requires_operator_opt_in"]})
    conditions = config.conditions_for(Action.WIDEN_RESULTS)
    assert Condition.REQUIRES_OPERATOR_OPT_IN in conditions
    assert Condition.REQUIRES_REMAINING_ATTEMPTS in conditions
    assert Condition.REQUIRES_RESULT_HEADROOM in conditions


def test_configuration_cannot_remove_an_inherent_condition() -> None:
    """An empty list is not a way to say "no conditions"."""
    config = PolicyConfig.from_mapping({"answer": []})
    assert config.conditions_for(Action.ANSWER) == frozenset({Condition.REQUIRES_EVIDENCE})


def test_the_condition_vocabulary_is_closed() -> None:
    with pytest.raises(ConfigurationError, match="unknown condition"):
        PolicyConfig.from_mapping({"answer": ["requires_admin_blessing"]})
    with pytest.raises(ConfigurationError, match="unknown action"):
        PolicyConfig.from_mapping({"escalate": []})


def test_permission_is_not_expressible_in_configuration() -> None:
    """No condition means "allowed", and the config has no field for one."""
    vocabulary = " ".join(str(c) for c in Condition)
    for word in ("permit", "allow", "authoris", "authoriz", "grant", "channel", "viewer"):
        assert word not in vocabulary
    fields = {f.name for f in dataclasses.fields(PolicyConfig)}
    assert fields == {"added_conditions"}


def test_a_configuration_that_conditions_abstention_is_rejected_at_load() -> None:
    """Every reachable state must keep one action needing no conditions."""
    stranded = PolicyConfig.from_mapping({"abstain": ["requires_operator_opt_in"]})
    with pytest.raises(ConfigurationError, match="unconditional action"):
        validate_config(stranded)
    with pytest.raises(ConfigurationError):
        CorrectivePolicy(stranded)


# --- deciding ----------------------------------------------------------


def test_the_preferred_action_is_taken_when_nothing_blocks_it() -> None:
    policy = CorrectivePolicy()
    assert policy.decide(Verdict.PARTIAL, permissive()).action is Action.WIDEN_RESULTS


def test_an_exhausted_attempt_allowance_blocks_corrective_actions() -> None:
    policy = CorrectivePolicy()
    decision = policy.decide(
        Verdict.IRRELEVANT, dataclasses.replace(permissive(), attempts_remaining=0)
    )
    assert decision.action is Action.ANSWER
    assert [b.action for b in decision.blocked] == [Action.REFORMULATE]
    assert decision.blocked[0].reason is BlockedReason.UNMET_CONDITION
    assert not decision.blocked_by_configuration


def test_with_no_evidence_and_no_attempts_the_run_abstains() -> None:
    policy = CorrectivePolicy()
    decision = policy.decide(
        Verdict.IRRELEVANT, ActionContext(evidence_count=0, attempts_remaining=0)
    )
    assert decision.action is Action.ABSTAIN
    assert {b.action for b in decision.blocked} == {Action.REFORMULATE, Action.ANSWER}


def test_a_configured_condition_is_reported_separately_from_an_inherent_one() -> None:
    """So an operator can tell a policy problem from a corpus problem."""
    policy = CorrectivePolicy(
        PolicyConfig.from_mapping({"reformulate": ["requires_operator_opt_in"]})
    )
    decision = policy.decide(Verdict.IRRELEVANT, permissive())
    assert decision.action is Action.ANSWER
    assert decision.blocked[0].reason is BlockedReason.CONFIGURED_CONDITION
    assert decision.blocked_by_configuration


def test_an_operator_opt_in_satisfies_the_condition_it_added() -> None:
    policy = CorrectivePolicy(
        PolicyConfig.from_mapping({"reformulate": ["requires_operator_opt_in"]})
    )
    context = dataclasses.replace(
        permissive(), operator_opt_in=frozenset({Action.REFORMULATE})
    )
    assert policy.decide(Verdict.IRRELEVANT, context).action is Action.REFORMULATE


class DenyReformulation:
    """A permission collaborator, of the kind configuration cannot reach."""

    def may(self, action: Action) -> bool:
        return action is not Action.REFORMULATE


def test_configuration_cannot_override_the_authorizer() -> None:
    """The two predicates come from two objects and combine conjunctively."""
    permissive_config = PolicyConfig.from_mapping({"reformulate": []})
    policy = CorrectivePolicy(permissive_config, DenyReformulation())
    decision = policy.decide(Verdict.IRRELEVANT, permissive())
    assert decision.action is not Action.REFORMULATE
    assert decision.blocked[0].reason is BlockedReason.NOT_PERMITTED


def test_the_authorizer_is_not_reachable_from_the_configuration() -> None:
    config = PolicyConfig.from_mapping({"reformulate": []})
    assert not hasattr(config, "authorizer")
    assert "authorizer" not in {f.name for f in dataclasses.fields(PolicyConfig)}
