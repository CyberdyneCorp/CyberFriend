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

A run may also be offered federated tools, and call one. Both happen once,
from the person's question, *before* anything is retrieved: asking after
retrieval would put content written by anyone in the server between the
question and the tools a run can see, which is the steering chain the
authorization layer exists to break. Which tools were offered is recorded in
the run, because an operator must be able to tell an empty offer from a
federation that is not wired at all.

A call is proposed by a model and disposed of by a guard. Nothing here
invokes anything: `FederatedSurface.invoke` is the loop's only route out, and
behind it authorization is re-checked, egress clearance is minted from the
asker's own question, and an audit entry is written whatever the outcome. The
loop never learns whether a refusal was a missing permit or a missing
confirmation, because it has no decision to make either way -- a refused,
failed or slow call is a run that answers from what it has.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

import structlog

from chatmemory.app.authorization import ActionOrigin, InvocationRequest
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
from chatmemory.app.reasoning.ports import (
    ExternalTool,
    Planner,
    Synthesizer,
    ToolCall,
    ToolCompletion,
    ToolDefinition,
    ToolSurface,
)
from chatmemory.app.reasoning.scope import retrieval_viewer
from chatmemory.ports.answers import Question

log = structlog.get_logger()

DEFAULT_MAX_SUB_QUESTIONS = 3

FEDERATION = "federation"
"""Decision name under which the offered federated tools are recorded."""

FEDERATION_CALL = "federation_call"
"""Decision name under which what came of those tools is recorded.

Separate from `FEDERATION` so a record distinguishes the three states an
operator has to tell apart: tools were offered and none was wanted, one was
wanted and refused, one was wanted and answered.
"""


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What a federated call produced, as much of it as a run may see.

    Deliberately not the invoker's own outcome: that carries the permit, the
    audit entry and any confirmation prompt, none of which the reasoning layer
    has business holding. What is left is what an answer needs -- the text, and
    which system said it.

    `detail` is for the operator record only. It never reaches a prompt and is
    never rendered to a requester: why a call was refused is exactly the hint
    a steered model would use to try the next thing.
    """

    invoked: bool = False
    source_system: str = ""
    text: str = ""
    attribution: str = ""
    detail: str = ""


@runtime_checkable
class FederatedSurface(Protocol):
    """A `ToolSurface` that can also call what it offered.

    One object, three methods, on purpose. A loop holding an invoker the
    surface did not produce could call a tool this question was never offered,
    and the invoke-time check would be the last thing standing between a
    steered model and somebody else's system. Here the offer and the call are
    routed from the same question against the same registration.

    A surface that only offers is still a valid collaborator: a deployment
    whose model cannot call tools, or that has not wired an invoker, gets
    exactly the behaviour the loop had before this existed.
    """

    def offer(self, question: str) -> Sequence[ExternalTool]: ...

    async def propose(
        self, question: str, tools: Sequence[ToolDefinition]
    ) -> ToolCompletion:
        """Ask the model whether one of `tools` should be called.

        Question and tool definitions only. There is no parameter for
        retrieved content, a document, or another tool's result, so nothing
        the corpus contains can nominate a tool or write its arguments.
        """
        ...

    async def invoke(self, request: InvocationRequest) -> ToolOutcome:
        """Authorize, call, audit. The loop's only route to another system."""
        ...


_CAUSE_PRECEDENCE = (
    TerminalCause.EVIDENCE_SUFFICIENT,
    TerminalCause.CONFIGURATION_BLOCKED,
    TerminalCause.ACCESS_BLOCKED,
    TerminalCause.BUDGET_EXHAUSTED,
    TerminalCause.NO_PROGRESS,
    TerminalCause.CORPUS_EMPTY,
)


def _driver_decision(name: str, outcome: str, detail: str = "") -> Decision:
    """A decision the loop itself took, and therefore paid no model call for.

    `Decision` rejects a cheap signal that claims model calls, so this is the
    only shape these may take.
    """
    return Decision(name, outcome, DecisionMaker.DRIVER, detail=detail)


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
        tools: ToolSurface | FederatedSurface | None = None,
    ) -> None:
        self._driver = driver
        self._planner = planner
        self._synthesizer = synthesizer
        self._max_sub_questions = max_sub_questions
        # Optional, and None is the whole of "this deployment has no
        # federation": a run then behaves exactly as it did before federation
        # existed, rather than taking a differently-shaped path through it.
        self._tools = tools
        # A surface that can only offer is a working surface. Deciding it here
        # rather than at the call site means the difference shows up once, as a
        # collaborator a run either has or has not, instead of as a check
        # somebody has to remember at each use.
        self._federated = tools if isinstance(tools, FederatedSurface) else None

    async def run(self, question: Question) -> RunOutcome:
        viewer = retrieval_viewer(question)
        spend = self._driver.new_ledger()
        evidence = EvidenceLedger()

        offered, offer_decision = self._offer_tools(question)
        # Before planning and before retrieval, so that nothing anyone in the
        # server wrote is in context when a call to another system is chosen.
        call_decisions = await self._call_tool(question, offered, spend, evidence)
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
        opening = () if offer_decision is None else (offer_decision,)
        leading = (*opening, *call_decisions, plan_decision)
        gathered = replace(combined, decisions=(*leading, *combined.decisions))
        if evidence.external and gathered.abstained:
            # The corpus found nothing, but the run is holding a result from
            # another system. Abstaining here would say "I couldn't find
            # anything" over evidence in hand, which is the one thing an
            # abstention must never mean; the cause stays `corpus_empty`, so
            # the record still says where the silence was.
            gathered = replace(gathered, abstained=False)
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

    def _offer_tools(
        self, question: Question
    ) -> tuple[Sequence[ExternalTool], Decision | None]:
        """Ask the tool surface what this question may see, and record it.

        A `None` decision means the deployment has no federation at all -- a
        run then carries no federation decision, which is how an operator
        reading a record tells "nothing was relevant" from "nothing is wired".

        Nothing is invoked here. An offer is a list of names and argument
        shapes; deciding that one of them should be called is a separate step
        with a separate record, because "the tool was there" and "the tool was
        used" are the two facts an incident needs told apart.
        """
        if self._tools is None:
            return (), None
        # The question, and only the question. Retrieved content is not in
        # scope here and must never become an argument to this call.
        offered: Sequence[ExternalTool] = self._tools.offer(question.text)
        names = tuple(sorted(t.qualified_name for t in offered))
        log.info("reasoning.federation.offered", count=len(names), tools=list(names))
        return offered, Decision(
            FEDERATION,
            f"{len(names)}_tools_offered",
            DecisionMaker.DRIVER,
            detail=", ".join(names),
        )

    async def _call_tool(
        self,
        question: Question,
        offered: Sequence[ExternalTool],
        spend: BudgetLedger,
        evidence: EvidenceLedger,
    ) -> tuple[Decision, ...]:
        """Ask the model whether a tool is wanted, and call the one it names.

        Once per run, whatever the model asks for: the question is put once,
        the answer is at most one call, and there is no second round in which
        a tool result could ask for another. That is the cost bound, and it is
        structural rather than configured -- a loop that could keep calling
        would be a bill nobody approved.

        Everything here degrades. A surface that cannot call, an offer with
        nothing in it, an exhausted budget, a model that fails, a refusal, a
        server that times out: each one ends with a decision on the record and
        a run that carries on to answer from the corpus.
        """
        surface = self._federated
        if surface is None or not offered:
            return ()
        reached = spend.exhausted()
        if reached is not None:
            # Checked before the model is asked, not after: the expensive half
            # of a tool call is the call, but a proposal a run cannot afford to
            # act on is money spent on nothing at all.
            return (_driver_decision(FEDERATION_CALL, f"budget_{reached}"),)

        proposal = await self._propose(surface, question, offered)
        if proposal is None:
            return (_driver_decision(FEDERATION_CALL, "proposal_failed"),)
        spend.charge_model_call(proposal.prompt_tokens)
        call = proposal.call
        if call is None:
            # The model read the question and wanted nothing external. That is
            # the common case and it is recorded, so an operator can see the
            # difference between a tool nobody wanted and a tool that failed.
            return (
                Decision(
                    FEDERATION_CALL,
                    "no_tool_requested",
                    DecisionMaker.MODEL,
                    model_calls=1,
                ),
            )
        requested = Decision(
            FEDERATION_CALL,
            "tool_requested",
            DecisionMaker.MODEL,
            model_calls=1,
            detail=call.name,
        )
        # Charged before the call, so a tool that hangs until its timeout costs
        # the run what a tool that answered would have.
        spend.charge_tool_call()
        return (requested, await self._invoke(surface, question, call, evidence))

    async def _propose(
        self,
        surface: FederatedSurface,
        question: Question,
        offered: Sequence[ExternalTool],
    ) -> ToolCompletion | None:
        """The model's request, or None if asking for it failed.

        The completion's own prose is discarded. This model has been given no
        evidence, so anything it wrote would be its own knowledge -- which is
        the one source an answer here may never rest on.
        """
        try:
            return await surface.propose(
                question.text, tuple(tool.definition for tool in offered)
            )
        except Exception as exc:  # noqa: BLE001 - any model failure degrades the same way
            log.warning("reasoning.federation.proposal_failed", error=str(exc))
            return None

    async def _invoke(
        self,
        surface: FederatedSurface,
        question: Question,
        call: ToolCall,
        evidence: EvidenceLedger,
    ) -> Decision:
        """Take the model's request to the guard, and fold in what comes back."""
        request = InvocationRequest(
            requester=question.asker.person,
            question=question.text,
            qualified_name=call.name,
            arguments=call.arguments,
            # A statement about the prompt this call came out of, not a label
            # chosen here: the model that named this tool had the person's
            # question in front of it and nothing else. Proposing after
            # retrieval would make this origin a lie, which is why the call is
            # proposed before anything is retrieved.
            origin=ActionOrigin.REQUESTER_REQUEST,
        )
        try:
            outcome = await surface.invoke(request)
        except Exception as exc:  # noqa: BLE001 - a broken tool must not break a question
            log.warning(
                "reasoning.federation.call_failed", tool=call.name, error=str(exc)
            )
            return _driver_decision(FEDERATION_CALL, "call_failed")
        if not outcome.invoked:
            # Refused, failed or timed out. The loop is told apart from those
            # deliberately: there is no action it could take on any of them
            # that is not "answer from what you have", and a loop that could
            # read a refusal reason is a loop that could try to route around it.
            log.info("reasoning.federation.not_invoked", tool=call.name)
            return _driver_decision(
                FEDERATION_CALL, "not_invoked", detail=outcome.detail
            )
        item = evidence.add_external(
            text=outcome.text,
            source_system=outcome.source_system,
            attribution=outcome.attribution,
        )
        log.info(
            "reasoning.federation.invoked",
            tool=call.name,
            source_system=item.source_system,
            window_id=item.window_id,
        )
        return _driver_decision(
            FEDERATION_CALL, "invoked", detail=outcome.source_system
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
