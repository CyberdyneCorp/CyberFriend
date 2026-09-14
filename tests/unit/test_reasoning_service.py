"""The answer service: routing, and one contract from both paths."""

from __future__ import annotations

import inspect
from dataclasses import fields

from chatmemory.app.reasoning.budgets import Budget
from chatmemory.app.reasoning.contract import AnswerPath, DecisionMaker, RunOutcome, RunRecord
from chatmemory.app.reasoning.fixed import CorrectiveDriver, FixedPath
from chatmemory.app.reasoning.loop import ReasoningLoop
from chatmemory.app.reasoning.service import ReasoningAnswerService
from chatmemory.ports.answers import Answer, AnswerService
from tests.unit.test_reasoning_fixed import (
    CitingSynthesizer,
    FakePlanner,
    FakeRetrieval,
    ScriptedCritic,
    evidence,
    question,
)


class Recorder:
    def __init__(self) -> None:
        self.records: list[RunRecord] = []

    def record(self, run: RunRecord) -> None:
        self.records.append(run)


def build_service(
    retrieval: FakeRetrieval, planner: FakePlanner | None = None
) -> tuple[ReasoningAnswerService, Recorder]:
    critic = ScriptedCritic()
    synthesizer = CitingSynthesizer()
    driver = CorrectiveDriver(retrieval, critic, budget=Budget(max_attempts=4))
    recorder = Recorder()
    service = ReasoningAnswerService(
        FixedPath(driver, synthesizer),
        ReasoningLoop(driver, planner or FakePlanner("first", "second"), synthesizer),
        recorder,
    )
    return service, recorder


def test_the_service_satisfies_the_answer_port() -> None:
    """Structural, since the port is a plain Protocol: same method, same
    parameter, and a coroutine -- which is all a caller of the port sees."""
    service, _ = build_service(FakeRetrieval([[evidence(1)]]))
    accepted: AnswerService = service
    assert inspect.iscoroutinefunction(accepted.answer)
    assert list(inspect.signature(accepted.answer).parameters) == ["question"]


async def test_a_routine_question_takes_the_fixed_path() -> None:
    retrieval = FakeRetrieval([[evidence(1)]])
    service, recorder = build_service(retrieval)
    await service.answer(question(text="what happened in infra yesterday"))

    assert recorder.records[0].path is AnswerPath.FIXED
    assert len(retrieval.calls) == 1


async def test_a_question_with_sub_goals_takes_the_loop() -> None:
    retrieval = FakeRetrieval([[evidence(1)], [evidence(2)]])
    service, recorder = build_service(retrieval)
    await service.answer(question(text="what do I need to do today"))

    assert recorder.records[0].path is AnswerPath.LOOP
    assert len(retrieval.calls) == 2


async def test_routing_is_recorded_as_costing_no_model_call() -> None:
    service, recorder = build_service(FakeRetrieval([[evidence(1)]]))
    await service.answer(question(text="what happened in infra"))

    route = recorder.records[0].decisions_named("route")[0]
    assert route.made_by is DecisionMaker.CLASSIFIER
    assert route.model_calls == 0


async def test_both_paths_return_the_identical_contract_shape() -> None:
    fixed_service, fixed_recorder = build_service(FakeRetrieval([[evidence(1)]]))
    loop_service, loop_recorder = build_service(FakeRetrieval([[evidence(1)], [evidence(2)]]))

    routine = await fixed_service.answer_run(question(text="what happened in infra"))
    multi = await loop_service.answer_run(question(text="what do I need to do today"))

    assert isinstance(routine, RunOutcome) and isinstance(multi, RunOutcome)
    assert {f.name for f in fields(routine.answer)} == {f.name for f in fields(multi.answer)}
    assert {f.name for f in fields(routine.record)} == {f.name for f in fields(multi.record)}
    for outcome in (routine, multi):
        assert isinstance(outcome.answer, Answer)
        assert outcome.answer.citations
        assert not outcome.answer.abstained
        assert outcome.record.status.successful
        assert outcome.record.spend.attempts >= 1
    assert fixed_recorder.records and loop_recorder.records
    assert fixed_recorder.records[0].path is not loop_recorder.records[0].path


async def test_answer_returns_only_the_answer_while_the_record_is_kept() -> None:
    service, recorder = build_service(FakeRetrieval([[evidence(1)]]))
    answer = await service.answer(question())

    assert isinstance(answer, Answer)
    assert len(recorder.records) == 1
