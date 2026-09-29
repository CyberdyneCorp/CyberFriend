"""The end-to-end time budget: one slow scenario fails it; a growing suite does not.

Regression: a fixed 120 s total failed main at 185 scenarios, every one passing
and none over 3 s. The budget was raised twice before for the same reason.
"""

from __future__ import annotations

from tests.e2e.conftest import SUITE_SECONDS_PER_TEST, TEST_BUDGET_SECONDS, over_budget


def test_a_large_suite_of_quick_scenarios_is_within_budget() -> None:
    durations = {f"tests/e2e/test_x.py::test_{i}": 0.65 for i in range(185)}
    assert over_budget(durations) == []


def test_one_slow_scenario_is_named() -> None:
    durations = {f"tests/e2e/test_x.py::test_{i}": 0.5 for i in range(20)}
    durations["tests/e2e/test_x.py::test_slow"] = TEST_BUDGET_SECONDS + 1
    [problem] = over_budget(durations)
    assert "test_slow" in problem and "per-scenario" in problem


def test_a_suite_whose_average_doubles_fails() -> None:
    durations = {f"tests/e2e/test_x.py::test_{i}": SUITE_SECONDS_PER_TEST + 0.5 for i in range(50)}
    [problem] = over_budget(durations)
    assert "suite took" in problem
