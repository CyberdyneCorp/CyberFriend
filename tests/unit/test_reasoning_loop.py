"""The reasoning loop: several lines of enquiry, one permission scope."""

from __future__ import annotations

from chatmemory.app.reasoning.budgets import Budget
from chatmemory.app.reasoning.contract import AnswerPath, DecisionMaker, RunStatus, TerminalCause
from chatmemory.app.reasoning.fixed import CorrectiveDriver
from chatmemory.app.reasoning.loop import ReasoningLoop
from chatmemory.app.reasoning.verdicts import Verdict
from tests.unit.test_reasoning_fixed import (
    CitingSynthesizer,
    FakePlanner,
    FakeRetrieval,
    ScriptedCritic,
    ch,
    evidence,
    question,
)


def build_loop(
    retrieval: FakeRetrieval,
    planner: FakePlanner,
    critic: ScriptedCritic | None = None,
    synthesizer: CitingSynthesizer | None = None,
    budget: Budget | None = None,
) -> ReasoningLoop:
    driver = CorrectiveDriver(
        retrieval, critic or ScriptedCritic(), budget=budget or Budget(max_attempts=6)
    )
    return ReasoningLoop(driver, planner, synthesizer or CitingSynthesizer())


async def test_a_question_with_several_sub_goals_retrieves_once_for_each() -> None:
    retrieval = FakeRetrieval([[evidence(1)], [evidence(2)]])
    loop = build_loop(retrieval, FakePlanner("what was asked of me", "what did I answer"))
    outcome = await loop.run(question(text="what do I need to do today"))

    assert len(retrieval.calls) == 2
    assert [q.text for _, q in retrieval.calls] == [
        "what was asked of me",
        "what did I answer",
    ]
    assert {c.message_id for c in outcome.answer.citations} == {10, 20}
    assert outcome.record.sub_questions == ("what was asked of me", "what did I answer")
    assert outcome.record.path is AnswerPath.LOOP


async def test_a_simple_lookup_is_not_expanded_into_several_retrievals() -> None:
    retrieval = FakeRetrieval([[evidence(1)]])
    loop = build_loop(retrieval, FakePlanner("what happened in infra"))
    outcome = await loop.run(question(text="what happened in infra"))

    assert len(retrieval.calls) == 1
    assert outcome.record.sub_questions == ("what happened in infra",)


async def test_a_planner_returning_nothing_asks_the_question_as_it_stands() -> None:
    retrieval = FakeRetrieval([[evidence(1)]])
    outcome = await build_loop(retrieval, FakePlanner()).run(question(text="who owns deploys"))

    assert [q.text for _, q in retrieval.calls] == ["who owns deploys"]
    assert outcome.record.sub_questions == ("who owns deploys",)


async def test_a_restricted_viewer_sees_no_restricted_content_at_any_step() -> None:
    """The permission scope is resolved once and holds for the whole run.

    The fake retrieval filters by the viewer it is handed, exactly as the
    store's SQL predicate does, so a step that reached past the viewer would
    show up as content in the answer.
    """
    private = [evidence(7, channel=300), evidence(8, channel=300)]
    retrieval = FakeRetrieval([[evidence(1), *private], private])
    loop = build_loop(retrieval, FakePlanner("first line", "second line"))
    outcome = await loop.run(question(text="what happened", visible=(100,)))

    assert all(viewer.visible_channels == {ch(100)} for viewer, _ in retrieval.calls)
    assert {c.channel for c in outcome.answer.citations} == {ch(100)}
    assert outcome.record.evidence_window_ids == (1,)


async def test_the_budget_is_a_whole_run_bound_not_a_per_step_allowance() -> None:
    retrieval = FakeRetrieval([[evidence(i)] for i in range(1, 10)])
    loop = build_loop(
        retrieval,
        FakePlanner("a", "b", "c", "d", "e"),
        budget=Budget(max_attempts=2),
    )
    outcome = await loop.run(question())

    assert len(retrieval.calls) == 2
    assert outcome.answer.partial
    assert outcome.record.cause is TerminalCause.BUDGET_EXHAUSTED


async def test_finding_nothing_across_every_line_is_a_successful_abstention() -> None:
    retrieval = FakeRetrieval([[]])
    loop = build_loop(
        retrieval, FakePlanner("a", "b"), critic=ScriptedCritic(Verdict.UNANSWERABLE)
    )
    outcome = await loop.run(question())

    assert outcome.answer.abstained
    assert outcome.record.status is RunStatus.ABSTAINED
    assert outcome.record.status.successful
    assert outcome.record.cause is TerminalCause.CORPUS_EMPTY


async def test_a_dependency_failure_mid_run_is_reported_as_a_failure() -> None:
    loop = build_loop(FakeRetrieval(fail=True), FakePlanner("a", "b"))
    outcome = await loop.run(question())

    assert outcome.record.status is RunStatus.FAILED
    assert outcome.record.cause is TerminalCause.DEPENDENCY_FAILED


async def test_planning_is_recorded_with_the_model_calls_it_cost() -> None:
    retrieval = FakeRetrieval([[evidence(1)]])
    outcome = await build_loop(retrieval, FakePlanner("a")).run(question())

    plan = outcome.record.decisions_named("plan")[0]
    assert plan.made_by is DecisionMaker.MODEL
    assert plan.model_calls == 1
