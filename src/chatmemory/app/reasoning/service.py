"""The answer service: one contract, two ways of producing it.

This is the real implementation of `ports.answers.AnswerService`. It routes,
runs, and records -- and does nothing else, so that the two paths stay
comparable and the routing decision stays visible in the record.

Retrieval scope is not this class's decision either: both paths derive their
viewer from the question's audience through `scope.retrieval_viewer`, and
neither accepts one from outside.

A question asked mid-conversation goes to the loop. Routing is lexical and
reads the question alone, and "and last month?" reads like a single lookup --
for nothing, because the fixed path retrieves with the question's own words.
Only the loop has a planner, and the planner is the one stage that can turn a
follow-up into a lookup with its subject spelled out.
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
    RunStatus,
)
from chatmemory.app.reasoning.fixed import CorrectiveDriver, FixedPath
from chatmemory.app.reasoning.loop import ReasoningLoop
from chatmemory.app.reasoning.ports import (
    ChatModel,
    Planner,
    RetrievalTool,
    Synthesizer,
    ToolSurface,
)
from chatmemory.app.reasoning.stages import ModelCritic, ModelPlanner, ModelSynthesizer
from chatmemory.app.routing import Route, RoutingDecision, classify
from chatmemory.ports.answers import Answer, Question

CONVERSATION_ROUTE = "conversation_route"
"""Decision name recorded when earlier turns send a question to the loop.

Separate from the classifier's own `route` decision, so a record still says
what the words alone would have chosen.
"""


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
        decisions = [_route_decision(routing)]
        route = routing.route
        if route is Route.FIXED and not question.memory.empty:
            route = Route.LOOP
            decisions.append(
                Decision(
                    CONVERSATION_ROUTE,
                    str(Route.LOOP),
                    DecisionMaker.DRIVER,
                    detail="permitted_memory_present",
                )
            )
        outcome = await (self._loop if route is Route.LOOP else self._fixed).run(question)

        # The fixed path searches the corpus and nothing else, and it carries
        # nearly every question -- which left every external tool unreachable
        # for exactly the questions they exist for. "Who wrote the novel Dune"
        # classifies as single-goal, found nothing in the corpus, and abstained
        # while a registered, routed, authorised Wikipedia tool sat unused.
        #
        # Escalating only on abstention keeps the ordering that matters: the
        # team's own conversations are always consulted first and always win
        # when they have an answer, and an outside source is reached only when
        # they have none. The loop already calls tools once per run under its
        # own budget, so this reuses that path rather than growing a second.
        if (
            route is Route.FIXED
            and outcome.record.status is RunStatus.ABSTAINED
            and self._loop.can_reach_outside
        ):
            decisions.append(
                Decision(
                    "escalation",
                    "fixed_abstained_trying_external_tools",
                    DecisionMaker.DRIVER,
                )
            )
            escalated = await self._loop.run(question)
            # Keep the escalation only when it actually produced an answer; an
            # escalation that also abstains says nothing the first run did not.
            if escalated.record.status is RunStatus.ANSWERED:
                outcome = escalated

        record = replace(
            outcome.record,
            decisions=(*decisions, *outcome.record.decisions),
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
    tools: ToolSurface | None = None,
) -> ReasoningAnswerService:
    """Wire the default composition.

    The two paths get separate drivers because they get separate budgets: the
    loop spends a whole-run allowance across several sub-questions, and giving
    it the fixed path's would leave nothing for correction.

    `tools` reaches the loop only. The fixed path answers a single-goal
    question from the corpus, where an external system is a second source to
    reconcile rather than a second sub-goal; leaving it out keeps the path
    that carries nearly all the traffic on one evidence kind. A deployment
    with no federation passes None and gets exactly today's behaviour.
    """
    critic = ModelCritic(model)
    writer = synthesizer or ModelSynthesizer(model)
    fixed = FixedPath(fixed_driver or CorrectiveDriver(retrieval, critic), writer)
    loop = ReasoningLoop(
        loop_driver or CorrectiveDriver(retrieval, critic, budget=LOOP_BUDGET),
        planner or ModelPlanner(model),
        writer,
        tools=tools,
    )
    return ReasoningAnswerService(fixed, loop, recorder)


LOOP_BUDGET = Budget(max_attempts=6, max_model_calls=16, max_tool_calls=24)
"""A whole-run allowance, not a per-sub-question one: a plan with more steps
than attempts stops early and says so, rather than letting the planner
multiply what a run may spend."""
