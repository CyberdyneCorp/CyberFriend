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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

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
from chatmemory.app.reasoning.ports import Planner, Synthesizer
from chatmemory.app.reasoning.scope import retrieval_viewer
from chatmemory.ports.answers import Question

DEFAULT_MAX_SUB_QUESTIONS = 3

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
    ) -> None:
        self._driver = driver
        self._planner = planner
        self._synthesizer = synthesizer
        self._max_sub_questions = max_sub_questions

    async def run(self, question: Question) -> RunOutcome:
        viewer = retrieval_viewer(question)
        spend = self._driver.new_ledger()
        evidence = EvidenceLedger()

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
        gathered = replace(combined, decisions=(plan_decision, *combined.decisions))
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
