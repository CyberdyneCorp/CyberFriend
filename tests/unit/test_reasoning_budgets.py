"""Budgets, and the two properties that make them worth having."""

from __future__ import annotations

import pytest

from chatmemory.app.reasoning.budgets import Budget, BudgetLedger, ExhaustedBy


class FakeClock:
    """A clock that only moves when a test moves it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_attempts_are_granted_up_to_the_bound_and_then_refused() -> None:
    ledger = BudgetLedger(Budget(max_attempts=2))
    assert ledger.begin_attempt() is None
    assert ledger.begin_attempt() is None
    assert ledger.begin_attempt() is ExhaustedBy.ATTEMPTS
    assert ledger.attempts == 2


def test_a_budget_of_one_attempt_still_performs_one() -> None:
    assert BudgetLedger(Budget(max_attempts=1)).begin_attempt() is None


def test_a_budget_of_no_attempts_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        Budget(max_attempts=0)


def test_model_call_token_and_tool_bounds_each_stop_a_run() -> None:
    ledger = BudgetLedger(Budget(max_model_calls=1, max_tool_calls=1, max_prompt_tokens=10))
    ledger.charge_model_call(prompt_tokens=4)
    assert ledger.exhausted() is ExhaustedBy.MODEL_CALLS

    ledger = BudgetLedger(Budget(max_tool_calls=1))
    ledger.charge_tool_call()
    assert ledger.exhausted() is ExhaustedBy.TOOL_CALLS

    ledger = BudgetLedger(Budget(max_prompt_tokens=10))
    ledger.charge_model_call(prompt_tokens=11)
    assert ledger.exhausted() is ExhaustedBy.PROMPT_TOKENS


def test_the_wall_clock_bound_needs_no_sleeping_to_test() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(Budget(max_seconds=5.0), clock)
    assert ledger.exhausted() is None
    clock.now = 5.1
    assert ledger.exhausted() is ExhaustedBy.WALL_CLOCK
    assert ledger.begin_attempt() is ExhaustedBy.WALL_CLOCK


def test_the_recursion_limit_is_derived_from_max_attempts() -> None:
    """A hardcoded framework limit silently caps a raised application limit,
    and surfaces as a recursion error rather than the attempts asked for."""
    small, large = Budget(max_attempts=3), Budget(max_attempts=50)
    assert large.recursion_limit > small.recursion_limit
    assert large.recursion_limit > large.max_attempts


def test_spend_reports_what_a_run_consumed() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(Budget(), clock)
    ledger.begin_attempt()
    ledger.charge_model_call(prompt_tokens=120)
    ledger.charge_tool_call()
    clock.now = 2.5
    spend = ledger.spend()
    assert (spend.attempts, spend.model_calls, spend.tool_calls) == (1, 1, 1)
    assert spend.prompt_tokens == 120
    assert spend.elapsed_seconds == 2.5
