"""A labelled set for the classifier, and the confusion matrix it produces.

The classifier is a new failure surface, so its errors are recorded here
rather than assumed absent. The two mistakes are not equivalent: a hard
question misrouted to the fixed path answers shallowly, while an easy one
misrouted to the loop only costs more. The asserted numbers are the current
behaviour; changing them should be a deliberate edit, not a surprise.

Honest caveat: these examples were written alongside the heuristics, so a
clean sweep here bounds nothing about production phrasing. Replacing this set
with sampled real questions is what would make the number mean something.
"""

from __future__ import annotations

from chatmemory.app.routing import LabelledQuestion, Route, confusion_matrix

EVAL_SET = (
    # --- routine: retrieval plus a filter ------------------------------
    LabelledQuestion("what happened in #infra yesterday", Route.FIXED),
    LabelledQuestion("who deployed the api last week", Route.FIXED),
    LabelledQuestion("what did sam say about the migration", Route.FIXED),
    LabelledQuestion("when did we agree the freeze starts", Route.FIXED),
    LabelledQuestion("what was decided about the rate limiter", Route.FIXED),
    LabelledQuestion("did we ever fix the flaky windowing test", Route.FIXED),
    LabelledQuestion("what did we say about bugs and features", Route.FIXED),
    LabelledQuestion("who is on call this week", Route.FIXED),
    # --- separable sub-goals, derived state, or an external system -----
    LabelledQuestion("what do I need to do today", Route.LOOP),
    LabelledQuestion("what did sam ask me and whether I replied", Route.LOOP),
    LabelledQuestion("is anything still open from last week", Route.LOOP),
    LabelledQuestion("compare what infra and platform decided about retries", Route.LOOP),
    LabelledQuestion("did anyone follow up on the auth bug", Route.LOOP),
    LabelledQuestion("what's the status of the linear ticket for the outage", Route.LOOP),
    LabelledQuestion("who asked me something? and what did I miss?", Route.LOOP),
    LabelledQuestion("catch me up on what happened while I was away", Route.LOOP),
)


def test_the_confusion_matrix_is_what_it_is() -> None:
    matrix = confusion_matrix(EVAL_SET)

    assert matrix.total == len(EVAL_SET)
    assert matrix.loop_correct == 8
    assert matrix.fixed_correct == 8
    assert matrix.routed_to_loop_but_fixed == 0
    assert matrix.routed_to_fixed_but_loop == 0
    assert matrix.accuracy == 1.0


def test_the_eval_set_is_balanced_enough_to_show_either_error() -> None:
    """A set of only routine questions would score a classifier that never
    routes to the loop at 100%."""
    loop_examples = [e for e in EVAL_SET if e.expected is Route.LOOP]
    fixed_examples = [e for e in EVAL_SET if e.expected is Route.FIXED]
    assert len(loop_examples) >= 6
    assert len(fixed_examples) >= 6


def test_a_classifier_that_always_says_fixed_is_caught_by_the_matrix() -> None:
    from chatmemory.app.routing import RoutingDecision

    matrix = confusion_matrix(EVAL_SET, lambda _text: RoutingDecision(Route.FIXED))
    assert matrix.routed_to_fixed_but_loop == 8
    assert matrix.accuracy == 0.5
