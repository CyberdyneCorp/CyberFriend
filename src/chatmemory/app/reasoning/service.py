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

Three kinds of question never reach the corpus at all, decided before either
path runs (see `routing` for how each is recognised):

*   a request to add or change an MCP server is refused and pointed at the
    admin console -- chat is not where the agent's reach is widened;
*   a current-price question goes to the market tools only, because a channel
    message quoting a price is not a price;
*   an explicit "search the web for" goes to external sources only, because
    the person has already said the corpus is not what they want.

Everything else is corpus first. External sources are consulted only when the
critic did not find the corpus evidence sufficient -- abstained or partial --
so a question the team's own conversations answer never leaves the server.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import structlog

from chatmemory.app.egress import (
    ISO_4217_CODES,
    MARKET_CRYPTO_PROVIDER,
    MARKET_FX_PROVIDER,
    MARKET_INDEX_PROVIDER,
)
from chatmemory.app.reasoning.budgets import Budget
from chatmemory.app.reasoning.contract import (
    AnswerPath,
    Decision,
    DecisionMaker,
    LoggingRunRecorder,
    RunOutcome,
    RunRecord,
    RunRecorder,
    RunStatus,
    TerminalCause,
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
from chatmemory.app.routing import (
    MarketQuestion,
    Route,
    RoutingDecision,
    classify,
    explicit_web_search,
    market_follow_up,
    market_question,
    mcp_change_request,
)
from chatmemory.ports.answers import Answer, Question

log = structlog.get_logger()

CONVERSATION_ROUTE = "conversation_route"
"""Decision name recorded when earlier turns send a question to the loop.

Separate from the classifier's own `route` decision, so a record still says
what the words alone would have chosen.
"""

EXTERNAL_ROUTE = "external_route"
"""Decision name for a question sent past the corpus before any retrieval."""

ESCALATION = "escalation"

MARKET_SERVERS = frozenset({MARKET_CRYPTO_PROVIDER, MARKET_FX_PROVIDER, MARKET_INDEX_PROVIDER})

MCP_CHANGE_REFUSAL = (
    "I can't add, remove or change MCP servers from chat. Connecting a server "
    "widens what I can reach, so it's done by an operator in the admin console, "
    "where the change is authenticated and recorded."
)

MARKET_UNAVAILABLE = (
    "I couldn't get a current figure from market data just now, and I won't "
    "give an older one or a price quoted in a channel instead. Try again in a "
    "moment."
)

NO_ADVICE = (
    "I don't give buy, sell or hold recommendations. Here is the current "
    "figure, for you to decide with:"
)
"""Put in front of the figure when a price question asks what to do.

A constant rather than an instruction to a model: the market answer is the
adapter's own figure lines, so there is no model prose in which a
recommendation could appear, and this sentence is the whole of what is said
about the question's "should I".
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
        direct = await self._answer_outside_corpus(question)
        if direct is not None:
            decision, outcome = direct
            return self._recorded(outcome, [*decisions, decision])
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
        if route is Route.LOOP:
            # Corpus first on the loop too. Its tool call is chosen before
            # retrieval, so a loop run allowed to call one would reach the web
            # for questions the corpus then answers; escalation below comes
            # back for the tools when the evidence falls short.
            outcome = await self._loop.run(question, consult_outside=False)
        else:
            outcome = await self._fixed.run(question)

        # The fixed path searches the corpus and nothing else, and it carries
        # nearly every question -- which left every external tool unreachable
        # for exactly the questions they exist for. "Who wrote the novel Dune"
        # classifies as single-goal, found nothing in the corpus, and abstained
        # while a registered, routed, authorised Wikipedia tool sat unused.
        #
        # Escalating on the critic's judgement, not only on abstention. When
        # retrieval returned loosely related messages the fixed path answered
        # from them, marked partial, and the web was never tried -- so a
        # general question got a colleague's tangent. Ordering still holds:
        # the team's conversations are consulted first and win whenever the
        # critic finds them sufficient, and only then is nothing sent outside.
        shortfall = _shortfall(outcome)
        if shortfall and self._loop.can_reach_outside:
            decisions.append(
                Decision(
                    ESCALATION,
                    f"{route}_{shortfall}_trying_external_tools",
                    DecisionMaker.DRIVER,
                )
            )
            escalated = await self._loop.run(question)
            if _improves_on(outcome, escalated):
                outcome = escalated

        return self._recorded(outcome, decisions)

    def _recorded(self, outcome: RunOutcome, decisions: list[Decision]) -> RunOutcome:
        record = replace(
            outcome.record,
            decisions=(*decisions, *outcome.record.decisions),
        )
        self._recorder.record(record)
        return RunOutcome(answer=outcome.answer, record=record)

    async def _answer_outside_corpus(
        self, question: Question
    ) -> tuple[Decision, RunOutcome] | None:
        """The questions the corpus must not answer, answered without it.

        Checked in order of what is at stake: a request to widen the agent's
        reach is refused before anything else could act on it, and a price
        question outranks a web request because "search the web for the BTC
        price" is still a price question with a better source.
        """
        text = question.text
        if mcp_change_request(text):
            log.info("reasoning.external_route", route="mcp_change_refused")
            return _route(EXTERNAL_ROUTE, "mcp_change_refused"), refusal(MCP_CHANGE_REFUSAL)
        market, asked = _market_request(question)
        if market is not None:
            outcome = await self._loop.run_external(asked, MARKET_SERVERS, verbatim=True)
            answer = outcome.answer
            if answer.abstained:
                answer = replace(answer, text=MARKET_UNAVAILABLE)
            if market.advisory:
                answer = replace(answer, text=f"{NO_ADVICE}\n{answer.text}")
            log.info("reasoning.external_route", route="market", kind=str(market.kind))
            return (
                _route(EXTERNAL_ROUTE, f"market_{market.kind}"),
                replace(outcome, answer=answer),
            )
        if explicit_web_search(text):
            log.info("reasoning.external_route", route="explicit_web_search")
            outcome = await self._loop.run_external(question)
            return _route(EXTERNAL_ROUTE, "explicit_web_search"), outcome
        return None


def _market_request(question: Question) -> tuple[MarketQuestion | None, Question]:
    """The market figure asked for, and the question to put to the market tools.

    A follow-up ("and now?") is put with its subject spelled out in
    closed-vocabulary terms, because the tool proposal sees the question and
    nothing else, and "and now?" alone names no instrument. Without this the
    follow-up went to the loop, and its planner -- the stage that does read
    memory -- turned it into a corpus search for the price.

    What is added is a term the market vocabulary admits, never remembered
    text, and the egress guard checks these providers by membership rather
    than rooting, so the addition cannot widen what leaves.
    """
    text = question.text
    direct = market_question(text, ISO_4217_CODES)
    if direct is not None:
        return direct, question
    # Remembered questions are the asker's own words as typed, never the
    # expanded text below, so a chain of follow-ups is walked back to the
    # price question it started from.
    previous = tuple(turn.question for turn in question.memory.turns)
    follow_up = market_follow_up(text, previous, ISO_4217_CODES)
    if follow_up is None:
        return None, question
    log.info("reasoning.market_follow_up", kind=str(follow_up.question.kind))
    return follow_up.question, replace(question, text=follow_up.text_for(text))


def _route(name: str, outcome: str) -> Decision:
    """A lexical routing decision: made by the classifier, costing no model call."""
    return Decision(name, outcome, DecisionMaker.CLASSIFIER)


def _shortfall(outcome: RunOutcome) -> str:
    """Why a corpus run's evidence falls short, or "" when it does not.

    A failed run is not a shortfall: retrieval was down, and an answer from
    the web would hide that behind something that looks like a result.
    """
    record = outcome.record
    if record.status is RunStatus.ABSTAINED:
        return "abstained"
    sufficient = record.cause is TerminalCause.EVIDENCE_SUFFICIENT
    if record.status is RunStatus.ANSWERED and not sufficient:
        return "evidence_insufficient"
    return ""


def _improves_on(original: RunOutcome, escalated: RunOutcome) -> bool:
    """Whether the escalated run is worth answering with instead.

    It must have answered. Over an abstention that is enough; over a partial
    corpus answer it must also hold something from outside, or it is the same
    partial answer bought twice.
    """
    if escalated.record.status is not RunStatus.ANSWERED:
        return False
    if original.record.status is RunStatus.ABSTAINED:
        return True
    return any(window_id < 0 for window_id in escalated.record.evidence_window_ids)


def refusal(text: str) -> RunOutcome:
    """An answer that consulted nothing, recorded as blocked by configuration."""
    return RunOutcome(
        # An empty set, not None: nothing was read, so the reply rests on no
        # channel, and None would claim that was never established.
        answer=Answer(text=text, consulted_channels=frozenset()),
        record=RunRecord(
            path=AnswerPath.FIXED,
            status=RunStatus.ANSWERED,
            cause=TerminalCause.CONFIGURATION_BLOCKED,
        ),
    )


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
