"""The classifier deciding which path answers a question."""

from __future__ import annotations

import pytest

from chatmemory.app.routing import Route, RoutingSignal, classify, signals_in


@pytest.mark.parametrize(
    "text",
    [
        "what happened in #infra yesterday",
        "who deployed the api last week",
        "did sam say anything about the migration?",
        "what was the decision on the rate limiter",
    ],
)
def test_routine_lookups_take_the_fixed_path(text: str) -> None:
    decision = classify(text)
    assert decision.route is Route.FIXED
    assert decision.signals == frozenset()
    assert decision.reason == "single_lookup"


@pytest.mark.parametrize(
    ("text", "signal"),
    [
        ("what did sam ask and whether anyone replied", RoutingSignal.SEPARABLE_SUBGOALS),
        ("who owns deploys? and what did they say?", RoutingSignal.MULTIPLE_QUESTIONS),
        ("compare what infra and platform decided", RoutingSignal.COMPARISON),
        ("what do I need to do today", RoutingSignal.DERIVED_STATE),
        ("did anyone follow up on the auth bug in linear", RoutingSignal.EXTERNAL_SYSTEM),
    ],
)
def test_each_signal_routes_to_the_loop(text: str, signal: RoutingSignal) -> None:
    decision = classify(text)
    assert decision.route is Route.LOOP
    assert signal in decision.signals


def test_classification_costs_no_model_call() -> None:
    """Routing every question through a model would make the cheap path
    expensive, which is the point of having one."""
    assert classify("what do I need to do today").model_calls == 0


def test_the_reason_names_the_signals_that_fired() -> None:
    decision = classify("what do I still owe the platform team, and what did I miss?")
    assert decision.route is Route.LOOP
    assert "derived_state" in decision.reason


def test_signal_detection_is_case_and_spacing_insensitive() -> None:
    assert signals_in("WHAT   DO I NEED TO DO today") == {RoutingSignal.DERIVED_STATE}


def test_a_conjunction_between_noun_phrases_is_not_a_sub_goal() -> None:
    """"bugs and features" is one lookup; the signal needs a verb after the
    connector, which is what distinguishes two lines of enquiry from one."""
    assert classify("what did we say about bugs and features").route is Route.FIXED
