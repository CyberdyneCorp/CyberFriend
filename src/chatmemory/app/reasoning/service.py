"""The answer service: one contract, two ways of producing it.

This is the real implementation of `ports.answers.AnswerService`. It routes,
runs, and records -- and does nothing else, so that the two paths stay
comparable and the routing decision stays visible in the record.

Retrieval scope is not this class's decision either: both paths derive their
viewer from the question's audience through `scope.retrieval_viewer`, and
neither accepts one from outside.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from chatmemory.app.reasoning.budgets import Budget
from chatmemory.app.reasoning.contract import (
    Decision,
    DecisionMaker,
    LoggingRunRecorder,
    RunOutcome,
    RunRecorder,
)
from chatmemory.app.reasoning.fixed import CorrectiveDriver, FixedPath
from chatmemory.app.reasoning.loop import ReasoningLoop
from chatmemory.app.reasoning.ports import (
    ChatModel,
    Planner,
    RetrievalTool,
    Synthesizer,
)
from chatmemory.app.reasoning.stages import ModelCritic, ModelPlanner, ModelSynthesizer
from chatmemory.app.routing import Route, RoutingDecision, classify
from chatmemory.ports.answers import Answer, Question


class ReasoningAnswerService:
    """Classify, answer by the chosen path, record what happened."""

    def __init__(
        self,
        fixed: FixedPath,
        loop: ReasoningLoop,
        recorder: RunRecorder | None = None,
        classifier: Callable[[str], RoutingDecision] = classify,
    ) -> None:
        self._fixed = fixed
        self._loop = loop
        self._recorder = recorder or LoggingRunRecorder()
        self._classify = classifier

    async def answer(self, question: Question) -> Answer:
        return (await self.answer_run(question)).answer

    async def answer_run(self, question: Question) -> RunOutcome:
        """The same work as `answer`, returning the operator record too."""
        routing = self._classify(question.text)
        outcome = await (
            self._loop if routing.route is Route.LOOP else self._fixed
        ).run(question)
        record = replace(
            outcome.record,
            decisions=(_route_decision(routing), *outcome.record.decisions),
        )
        self._recorder.record(record)
        return RunOutcome(answer=outcome.answer, record=record)


def _route_decision(routing: RoutingDecision) -> Decision:
    """Routing is lexical, so this records zero model calls -- and the
    `Decision` constructor rejects the record if that ever stops being true."""
    return Decision(
        "route",
        str(routing.route),
        DecisionMaker.CLASSIFIER,
        model_calls=routing.model_calls,
        detail=routing.reason,
    )


def build_answer_service(
    retrieval: RetrievalTool,
    model: ChatModel,
    *,
    planner: Planner | None = None,
    synthesizer: Synthesizer | None = None,
    fixed_driver: CorrectiveDriver | None = None,
    loop_driver: CorrectiveDriver | None = None,
    recorder: RunRecorder | None = None,
) -> ReasoningAnswerService:
    """Wire the default composition.

    The two paths get separate drivers because they get separate budgets: the
    loop spends a whole-run allowance across several sub-questions, and giving
    it the fixed path's would leave nothing for correction.
    """
    critic = ModelCritic(model)
    writer = synthesizer or ModelSynthesizer(model)
    fixed = FixedPath(fixed_driver or CorrectiveDriver(retrieval, critic), writer)
    loop = ReasoningLoop(
        loop_driver or CorrectiveDriver(retrieval, critic, budget=LOOP_BUDGET),
        planner or ModelPlanner(model),
        writer,
    )
    return ReasoningAnswerService(fixed, loop, recorder)


LOOP_BUDGET = Budget(max_attempts=6, max_model_calls=16, max_tool_calls=24)
"""A whole-run allowance, not a per-sub-question one: a plan with more steps
than attempts stops early and says so, rather than letting the planner
multiply what a run may spend."""
