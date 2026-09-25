"""The fixed corrective path, and the driver both paths share.

Topology: `retrieve -> evaluate -> {answer | correct -> retrieve} -> answer`.
This is not a reasoning loop. It has no planning step, the model never
chooses what happens next, and the number of retrievals it can perform is a
constant the driver holds. It carries nearly all the traffic; the loop exists
for the minority of questions with separable sub-goals.

The driver owns the budget. `CorrectivePolicy` is never handed the ledger, so
a policy that asks for another attempt forever still stops here -- and the
test for that runs with a policy configured to do exactly that.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from chatmemory.app.reasoning.budgets import Budget, BudgetLedger
from chatmemory.app.reasoning.contract import (
    AnswerPath,
    Decision,
    DecisionMaker,
    RunOutcome,
    RunRecord,
    RunStatus,
    TerminalCause,
    abstention_answer,
    failure_answer,
)
from chatmemory.app.reasoning.errors import RetrievalUnavailable
from chatmemory.app.reasoning.evidence import Evidence, EvidenceLedger
from chatmemory.app.reasoning.gate import RelevanceGate
from chatmemory.app.reasoning.policy import (
    MAX_RESULT_LIMIT,
    Action,
    ActionContext,
    BlockedAction,
    CorrectivePolicy,
    PolicyDecision,
    apply_action,
)
from chatmemory.app.reasoning.ports import Critic, RetrievalTool, Synthesizer
from chatmemory.app.reasoning.scope import retrieval_viewer
from chatmemory.app.reasoning.stages import prompt_context
from chatmemory.app.reasoning.verdicts import Assessment, Verdict
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.domain.search import SearchQuery
from chatmemory.ports.answers import Answer, Question

DEFAULT_LIMIT = 20

SUFFICIENCY = "sufficiency"
"""Decision name under which every sufficiency judgement is recorded.

Provenance is read back by this name, so the cheap-signal cases -- an empty
result, the relevance gate -- are directly comparable with the model case.
"""

SYNTHESIS_STAGE = "synthesis"
"""Stage name under which every synthesis model call is recorded."""


@dataclass(frozen=True, slots=True)
class GatherResult:
    """What one corrective cycle concluded, before any answer is written."""

    cause: TerminalCause
    access_blocked: bool = False
    abstained: bool = False
    queries: tuple[str, ...] = ()
    decisions: tuple[Decision, ...] = ()
    blocked_actions: tuple[BlockedAction, ...] = ()

    @property
    def has_answerable_evidence(self) -> bool:
        return self.cause is TerminalCause.EVIDENCE_SUFFICIENT


def initial_query(question_text: str, limit: int = DEFAULT_LIMIT) -> SearchQuery:
    """Intent only. No channel set: permission is not a field on this object."""
    return SearchQuery(text=question_text, limit=limit)


class CorrectiveDriver:
    """Retrieve, evaluate, correct -- within bounds it alone enforces."""

    def __init__(
        self,
        retrieval: RetrievalTool,
        critic: Critic,
        policy: CorrectivePolicy | None = None,
        gate: RelevanceGate | None = None,
        budget: Budget | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retrieval = retrieval
        self._critic = critic
        self._policy = policy or CorrectivePolicy()
        self._gate = gate or RelevanceGate()
        self._budget = budget or Budget()
        self._clock = clock

    @property
    def budget(self) -> Budget:
        return self._budget

    def new_ledger(self) -> BudgetLedger:
        return BudgetLedger(self._budget, self._clock)

    async def gather(
        self,
        question_text: str,
        viewer: Viewer,
        query: SearchQuery,
        spend: BudgetLedger,
        evidence: EvidenceLedger,
    ) -> GatherResult:
        """Run the cycle until the driver, the policy, or progress stops it."""
        state = _CycleState()
        current = query

        while True:
            stop = self._before_retrieval(current, spend, evidence, state)
            if stop is not None:
                return state.finish(stop)

            state.queries.append(current.text)
            result = await self._retrieval.retrieve(viewer, current)
            spend.charge_tool_call()
            state.access_blocked = state.access_blocked or result.access_blocked
            net_new = evidence.add(result.items, result.source_system)

            if state.rounds and not net_new:
                # A corrective round that returned only what the run already
                # held has not moved. Another attempt would cost money to
                # arrive in the same place.
                state.decisions.append(_driver_decision("stop", "no_new_evidence"))
                return state.finish(TerminalCause.NO_PROGRESS)

            assessment = await self._assess(question_text, current, result.items, spend, state)
            decision = self._policy.decide(
                assessment.verdict, self._context(current, evidence, spend)
            )
            state.blocked.extend(decision.blocked)
            state.decisions.append(
                Decision(
                    "correction",
                    str(decision.action),
                    DecisionMaker.POLICY,
                    detail=str(assessment.verdict),
                )
            )

            if decision.action is Action.ANSWER:
                return state.finish(_answer_cause(assessment.verdict, decision, spend))
            if decision.action is Action.ABSTAIN:
                state.abstained = True
                return state.finish(
                    _abstention_cause(decision.blocked_by_configuration, state.access_blocked)
                )

            current = apply_action(current, decision.action, assessment.suggested_query)
            state.rounds += 1

    def _before_retrieval(
        self,
        query: SearchQuery,
        spend: BudgetLedger,
        evidence: EvidenceLedger,
        state: _CycleState,
    ) -> TerminalCause | None:
        """The two driver-side stops, checked before a retrieval is issued."""
        if not evidence.note_query(query):
            # Substantially the same retrieval as one already issued.
            state.decisions.append(_driver_decision("stop", "equivalent_query"))
            return TerminalCause.NO_PROGRESS
        reached = spend.begin_attempt()
        if reached is not None:
            state.decisions.append(_driver_decision("stop", f"budget_{reached}"))
            return TerminalCause.BUDGET_EXHAUSTED
        return None

    def _context(
        self, query: SearchQuery, evidence: EvidenceLedger, spend: BudgetLedger
    ) -> ActionContext:
        return ActionContext(
            evidence_count=len(evidence),
            # Reported to the policy so it can decline a corrective action it
            # could not complete. The driver re-checks regardless, so a
            # context that lied would buy the policy nothing.
            attempts_remaining=max(0, self._budget.max_attempts - spend.attempts),
            has_time_range=query.since is not None,
            result_headroom=query.limit < MAX_RESULT_LIMIT,
        )

    async def _assess(
        self,
        question_text: str,
        query: SearchQuery,
        items: Sequence[Evidence],
        spend: BudgetLedger,
        state: _CycleState,
    ) -> Assessment:
        """Judge the candidates, paying for a model call only when one is needed."""
        if not items:
            state.decisions.append(
                Decision(SUFFICIENCY, Verdict.EMPTY, DecisionMaker.EMPTY_RESULT,
                         detail="retrieval returned no candidates")
            )
            return Assessment(Verdict.EMPTY)

        gate = self._gate.evaluate(items)
        if gate.skip_evaluation:
            state.decisions.append(
                Decision(
                    SUFFICIENCY,
                    Verdict.IRRELEVANT,
                    DecisionMaker.RELEVANCE_GATE,
                    detail=f"{gate.reason}: best score {gate.best_score}",
                )
            )
            return Assessment(Verdict.IRRELEVANT, score=gate.best_score or 0.0)

        started = spend.now()
        assessment = await self._critic.assess(question_text, query, items)
        spend.charge_model_call(
            assessment.prompt_tokens,
            assessment.model_calls,
            stage=SUFFICIENCY,
            model=assessment.model,
            completion_tokens=assessment.completion_tokens,
            started_at=started,
        )
        state.decisions.append(
            Decision(
                SUFFICIENCY,
                assessment.verdict,
                DecisionMaker.MODEL if assessment.model_calls else DecisionMaker.HEURISTIC,
                model_calls=assessment.model_calls,
            )
        )
        return assessment


@dataclass
class _CycleState:
    """Mutable bookkeeping for one cycle, sealed into a frozen result."""

    decisions: list[Decision] = field(default_factory=list)
    blocked: list[BlockedAction] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    access_blocked: bool = False
    abstained: bool = False
    rounds: int = 0

    def finish(self, cause: TerminalCause) -> GatherResult:
        return GatherResult(
            cause=cause,
            access_blocked=self.access_blocked,
            abstained=self.abstained,
            queries=tuple(self.queries),
            decisions=tuple(self.decisions),
            blocked_actions=tuple(self.blocked),
        )


def _answer_cause(
    verdict: Verdict, decision: PolicyDecision, spend: BudgetLedger
) -> TerminalCause:
    """Why the run stopped retrieving and answered.

    Answering is also the fallback when a corrective action is unavailable,
    and that is not the same as having found enough: recording it as such
    would hide a truncated run behind a satisfied one, and the answer would
    not be marked partial.
    """
    if verdict is Verdict.SUFFICIENT:
        return TerminalCause.EVIDENCE_SUFFICIENT
    if decision.blocked_by_configuration:
        return TerminalCause.CONFIGURATION_BLOCKED
    if spend.exhausted() is not None:
        return TerminalCause.BUDGET_EXHAUSTED
    return TerminalCause.NO_PROGRESS


def _driver_decision(name: str, outcome: str) -> Decision:
    return Decision(name, outcome, DecisionMaker.DRIVER)


def _abstention_cause(configuration_blocked: bool, access_blocked: bool) -> TerminalCause:
    """Why the run ended with nothing -- for the operator record only.

    All three causes produce the identical reply; only the record differs.
    """
    if configuration_blocked:
        return TerminalCause.CONFIGURATION_BLOCKED
    return TerminalCause.ACCESS_BLOCKED if access_blocked else TerminalCause.CORPUS_EMPTY


async def write_answer(
    synthesizer: Synthesizer,
    question: Question,
    evidence: EvidenceLedger,
    spend: BudgetLedger,
    decisions: list[Decision],
    partial: bool,
) -> Answer:
    """Turn held evidence into a grounded answer, or abstain.

    Citations are resolved against evidence the run actually holds: a window
    id the model names but the run never retrieved is not evidence of
    anything, so it cannot become a citation. An answer left with no
    citations at all is not delivered as an answer -- saying nothing was
    found is better than asserting something nothing supports.
    """
    # The asker's profile and permitted memory ride along as fenced context.
    # They cannot become citations: citations are resolved below against the
    # window ids this run retrieved, and memory carries none.
    started = spend.now()
    grounded = await synthesizer.synthesize(
        question.text, evidence.items, prompt_context(question)
    )
    spend.charge_model_call(
        grounded.prompt_tokens,
        grounded.model_calls,
        stage=SYNTHESIS_STAGE,
        model=grounded.model,
        completion_tokens=grounded.completion_tokens,
        started_at=started,
    )
    citations = evidence.citations(grounded.cited_window_ids)
    if not citations:
        decisions.append(_driver_decision("grounding", "no_resolvable_citations"))
        return abstention_answer()
    return Answer(text=grounded.text, citations=citations, partial=partial)


class FixedPath:
    """The default path. One contract, produced without a reasoning loop."""

    def __init__(self, driver: CorrectiveDriver, synthesizer: Synthesizer) -> None:
        self._driver = driver
        self._synthesizer = synthesizer

    async def run(self, question: Question) -> RunOutcome:
        viewer = retrieval_viewer(question)
        spend = self._driver.new_ledger()
        evidence = EvidenceLedger()
        try:
            gathered = await self._driver.gather(
                question.text, viewer, initial_query(question.text), spend, evidence
            )
        except RetrievalUnavailable as exc:
            return failed_run(AnswerPath.FIXED, spend, str(exc))
        return await finish_run(
            AnswerPath.FIXED, question, gathered, evidence, spend, self._synthesizer
        )


async def finish_run(
    path: AnswerPath,
    question: Question,
    gathered: GatherResult,
    evidence: EvidenceLedger,
    spend: BudgetLedger,
    synthesizer: Synthesizer,
    sub_questions: tuple[str, ...] = (),
) -> RunOutcome:
    """Build the shared contract from a finished cycle. Used by both paths."""
    decisions = list(gathered.decisions)
    answer = abstention_answer()
    if len(evidence) and not gathered.abstained:
        # A run that ran out of attempts or stopped making progress still
        # answers from what it holds, marked partial.
        answer = await write_answer(
            synthesizer,
            question,
            evidence,
            spend,
            decisions,
            partial=not gathered.has_answerable_evidence,
        )
    status = RunStatus.ABSTAINED if answer.abstained else RunStatus.ANSWERED
    return RunOutcome(
        # Every channel the run held evidence from, cited or not. Memory
        # records this as the turn's provenance: a sentence can paraphrase a
        # window without citing it, and must not outlive a revocation for it.
        answer=replace(answer, consulted_channels=consulted_channels(evidence)),
        # Carried for tracing only; see RunOutcome.evidence.
        evidence=evidence.items,
        record=RunRecord(
            path=path,
            status=status,
            cause=_recorded_cause(gathered, answer.abstained),
            spend=spend.spend(),
            decisions=tuple(decisions),
            queries=gathered.queries,
            sub_questions=sub_questions,
            evidence_window_ids=tuple(sorted(evidence.window_ids)),
            blocked_actions=gathered.blocked_actions,
        ),
    )


def _recorded_cause(gathered: GatherResult, abstained: bool) -> TerminalCause:
    """What an operator should act on, for a run that produced no answer.

    A run that ends with nothing is reported as one of three things an
    operator can act on: configuration, access, or the corpus. What
    mechanically ended the cycle -- a budget, a lack of progress -- stays in
    the decision trail, where it belongs: it is the stopping rule, not the
    reason there was nothing to say.

    None of this reaches the requester. The reply is the same constant for
    all three, produced by the same path.
    """
    if not abstained or gathered.cause is TerminalCause.CONFIGURATION_BLOCKED:
        return gathered.cause
    return TerminalCause.ACCESS_BLOCKED if gathered.access_blocked else TerminalCause.CORPUS_EMPTY


def consulted_channels(evidence: EvidenceLedger) -> frozenset[ChannelRef]:
    """The channels a run's evidence came from, external sources included.

    External evidence is filed under `ChannelRef(source_system, 0)`, and it is
    kept here rather than filtered: whether such a source can be re-checked
    later is memory's decision to make, and it cannot make it about a channel
    it was never told about.
    """
    return frozenset(item.channel for item in evidence.items)


def failed_run(path: AnswerPath, spend: BudgetLedger, detail: str) -> RunOutcome:
    """A dependency failure. Reported as a failure, never as "found nothing"."""
    return RunOutcome(
        # Nothing was retrieved, so the reply rests on no channel.
        answer=replace(failure_answer(), consulted_channels=frozenset()),
        record=RunRecord(
            path=path,
            status=RunStatus.FAILED,
            cause=TerminalCause.DEPENDENCY_FAILED,
            spend=spend.spend(),
            decisions=(Decision("stop", "retrieval_unavailable", DecisionMaker.DRIVER,
                                detail=detail),),
        ),
    )
