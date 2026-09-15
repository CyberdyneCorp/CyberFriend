"""The reasoning loop: the minority path, for questions with sub-goals.

It plans a question into separable lines of enquiry and runs the same
corrective cycle for each, against **one shared budget**. The budget is a
whole-run bound rather than a per-step allowance, deliberately: a plan with
more steps than the run has attempts stops early and says so, which is
honest, whereas a per-step budget would let the planner multiply the cost of
a run by emitting more steps.

Everything else is the fixed path's machinery, unchanged. The viewer is
resolved once, before planning, and threaded through every retrieval, so no
step of a run can reach content the requester could not read at step one.

A run may also be offered federated tools. They are requested once, from the
person's question, *before* anything is retrieved: asking after retrieval
would put content written by anyone in the server between the question and
the tools a run can see, which is the steering chain the authorization layer
exists to break. Which tools those are is recorded in the run, because an
operator must be able to tell an empty offer from a federation that is not
wired at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import structlog

from chatmemory.app.reasoning.budgets import BudgetLedger
from chatmemory.app.reasoning.contract import (
    AnswerPath,
    Decision,
    DecisionMaker,
    RunOutcome,
    TerminalCause,
)
from chatmemory.app.reasoning.errors import RetrievalUnavailable
from chatmemory.app.reasoning.evidence import EvidenceLedger
from chatmemory.app.reasoning.fixed import (
    CorrectiveDriver,
    GatherResult,
    failed_run,
    finish_run,
    initial_query,
)
from chatmemory.app.reasoning.ports import ExternalTool, Planner, Synthesizer, ToolSurface
from chatmemory.app.reasoning.scope import retrieval_viewer
from chatmemory.ports.answers import Question

log = structlog.get_logger()

DEFAULT_MAX_SUB_QUESTIONS = 3

FEDERATION = "federation"
"""Decision name under which the offered federated tools are recorded."""

_CAUSE_PRECEDENCE = (
    TerminalCause.EVIDENCE_SUFFICIENT,
    TerminalCause.CONFIGURATION_BLOCKED,
    TerminalCause.ACCESS_BLOCKED,
    TerminalCause.BUDGET_EXHAUSTED,
    TerminalCause.NO_PROGRESS,
    TerminalCause.CORPUS_EMPTY,
)


def combine(results: Sequence[GatherResult]) -> GatherResult:
    """Fold the per-sub-question cycles into the one shape both paths return.

    The reported cause is the most actionable one an operator could act on:
    evidence found anywhere makes the run answerable; otherwise a
    configuration or access cause outranks a plain empty corpus, because
    those are the two an operator can do something about.
    """
    if not results:
        return GatherResult(TerminalCause.CORPUS_EMPTY, abstained=True)
    causes = {r.cause for r in results}
    cause = next((c for c in _CAUSE_PRECEDENCE if c in causes), TerminalCause.CORPUS_EMPTY)
    return GatherResult(
        cause=cause,
        access_blocked=any(r.access_blocked for r in results),
        abstained=all(r.abstained for r in results),
        queries=tuple(q for r in results for q in r.queries),
        decisions=tuple(d for r in results for d in r.decisions),
        blocked_actions=tuple(b for r in results for b in r.blocked_actions),
    )


class ReasoningLoop:
    """Plan, gather per sub-question, answer once from everything gathered."""

    def __init__(
        self,
        driver: CorrectiveDriver,
        planner: Planner,
        synthesizer: Synthesizer,
        max_sub_questions: int = DEFAULT_MAX_SUB_QUESTIONS,
        tools: ToolSurface | None = None,
    ) -> None:
        self._driver = driver
        self._planner = planner
        self._synthesizer = synthesizer
        self._max_sub_questions = max_sub_questions
        # Optional, and None is the whole of "this deployment has no
        # federation": a run then behaves exactly as it did before federation
        # existed, rather than taking a differently-shaped path through it.
        self._tools = tools

    async def run(self, question: Question) -> RunOutcome:
        viewer = retrieval_viewer(question)
        spend = self._driver.new_ledger()
        evidence = EvidenceLedger()

        offer_decision = self._offer_tools(question)
        plan_decision, sub_questions = await self._plan(question, spend)
        results: list[GatherResult] = []
        try:
            for sub in sub_questions:
                results.append(
                    await self._driver.gather(
                        sub, viewer, initial_query(sub), spend, evidence
                    )
                )
                if spend.exhausted() is not None:
                    break
        except RetrievalUnavailable as exc:
            return failed_run(AnswerPath.LOOP, spend, str(exc))

        combined = combine(results)
        leading = (plan_decision,) if offer_decision is None else (offer_decision, plan_decision)
        gathered = replace(combined, decisions=(*leading, *combined.decisions))
        if len(results) < len(sub_questions):
            # The run stopped before every planned line of enquiry ran.
            # Whatever it found does not cover the plan, and the answer says
            # so rather than presenting a truncated search as a complete one.
            gathered = replace(gathered, cause=TerminalCause.BUDGET_EXHAUSTED)
        return await finish_run(
            AnswerPath.LOOP,
            question,
            gathered,
            evidence,
            spend,
            self._synthesizer,
            sub_questions=sub_questions,
        )

    def _offer_tools(self, question: Question) -> Decision | None:
        """Ask the tool surface what this question may see, and record it.

        `None` when the deployment has no federation at all -- a run then
        carries no federation decision, which is how an operator reading a
        record tells "nothing was relevant" from "nothing is wired".

        Nothing is invoked here. An offer is a list of names; a call needs
        arguments, and the only thing entitled to produce those is a model
        answering the person's question through a tool-calling shape
        `ChatModel` does not yet have. Until it does, this is the seam that
        is wired and the one that is missing -- and the run record says so
        rather than a comment nobody reads.
        """
        if self._tools is None:
            return None
        # The question, and only the question. Retrieved content is not in
        # scope here and must never become an argument to this call.
        offered: Sequence[ExternalTool] = self._tools.offer(question.text)
        names = tuple(sorted(t.qualified_name for t in offered))
        log.info("reasoning.federation.offered", count=len(names), tools=list(names))
        return Decision(
            FEDERATION,
            f"{len(names)}_tools_offered",
            DecisionMaker.DRIVER,
            detail=", ".join(names),
        )

    async def _plan(
        self, question: Question, spend: BudgetLedger
    ) -> tuple[Decision, tuple[str, ...]]:
        """Decompose the question, or fall back to asking it as it stands.

        A planner that returns one sub-question means one retrieval line: a
        simple lookup is never expanded into several just because it reached
        this path.
        """
        plan = await self._planner.plan(question.text, self._max_sub_questions)
        spend.charge_model_call(plan.prompt_tokens, plan.model_calls)
        sub_questions = tuple(plan.sub_questions[: self._max_sub_questions]) or (question.text,)
        decision = Decision(
            "plan",
            f"{len(sub_questions)}_sub_questions",
            DecisionMaker.MODEL if plan.model_calls else DecisionMaker.HEURISTIC,
            model_calls=plan.model_calls,
        )
        return decision, sub_questions
