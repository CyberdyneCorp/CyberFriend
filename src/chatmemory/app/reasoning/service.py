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
from datetime import datetime

import structlog

from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.egress import (
    CHAIN_BALANCES_PROVIDER,
    DEFI_POSITIONS_PROVIDER,
    ISO_4217_CODES,
    MARKET_CRYPTO_PROVIDER,
    MARKET_FX_PROVIDER,
    MARKET_INDEX_PROVIDER,
)
from chatmemory.app.language import Language, detect
from chatmemory.app.reasoning.budgets import Budget
from chatmemory.app.reasoning.contract import (
    AnswerPath,
    Decision,
    DecisionMaker,
    LoggingRunRecorder,
    NoRunTracer,
    RunOutcome,
    RunRecord,
    RunRecorder,
    RunStatus,
    RunTrace,
    RunTracer,
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
    DefiQuestion,
    MarketQuestion,
    PositionKind,
    Route,
    RoutingDecision,
    classify,
    defi_question,
    explicit_web_search,
    market_follow_up,
    market_question,
    mcp_change_request,
    time_question,
    wallet_question,
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
CHAIN_SERVERS = frozenset({CHAIN_BALANCES_PROVIDER})
DEFI_SERVERS = frozenset({DEFI_POSITIONS_PROVIDER})
DEFI_TOOLS = {
    PositionKind.LIQUIDITY: f"{DEFI_POSITIONS_PROVIDER}:liquidity_positions",
    PositionKind.LENDING: f"{DEFI_POSITIONS_PROVIDER}:lending_positions",
    PositionKind.BOTH: f"{DEFI_POSITIONS_PROVIDER}:defi_positions",
}
"""Exactly one tool per kind of question. The route decides what is read; the
model only copies the address into the call."""
"""The only server a wallet question may reach. Narrowed for the same reason
the market route is: the offer the call is held to is this set, so a wallet
question cannot end up searching the web instead."""

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


WALLET_ADDRESS_MISSING = (
    "Which wallet? Give me the address (it starts with `0x`) and I'll check "
    "Ethereum, Base and Arbitrum.\n"
    "I can only look up an address you type here - not one I found in a "
    "channel."
)
"""Said when somebody asks about a wallet without naming one.

An answer, not a search. No channel holds a live balance, so searching for one
returns whatever a colleague once wrote about wallets and presents it as the
asker's own -- which is the failure this whole route exists to stop.
"""

WALLET_UNAVAILABLE = (
    "I couldn't read that address just now - the chain endpoints didn't "
    "answer. Try again in a moment."
)
"""Distinct from "holds nothing", deliberately. Reporting an unreachable
endpoint as an empty wallet is the one wrong answer somebody would act on."""


class ReasoningAnswerService:
    """Classify, answer by the chosen path, record what happened."""

    def __init__(
        self,
        fixed: FixedPath,
        loop: ReasoningLoop,
        recorder: RunRecorder | None = None,
        classifier: Callable[[str], RoutingDecision] = classify,
        tracer: RunTracer | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._fixed = fixed
        self._loop = loop
        self._recorder = recorder or LoggingRunRecorder()
        self._classify = classifier
        self._tracer = tracer or NoRunTracer()
        self._clock = clock

    async def answer(self, question: Question) -> Answer:
        return (await self.answer_run(question)).answer

    async def answer_run(self, question: Question) -> RunOutcome:
        """The same work as `answer`, returning the operator record too."""
        routing = self._classify(question.text)
        decisions = [_route_decision(routing)]
        direct = await self._answer_outside_corpus(question)
        if direct is not None:
            decision, outcome = direct
            return await self._recorded(question, outcome, [*decisions, decision])
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

        return await self._recorded(question, outcome, decisions)

    async def _recorded(
        self, question: Question, outcome: RunOutcome, decisions: list[Decision]
    ) -> RunOutcome:
        record = replace(
            outcome.record,
            decisions=(*decisions, *outcome.record.decisions),
        )
        self._recorder.record(record)
        # After the record, and never in its place: the log line is what an
        # operator watches live, and must not depend on a remote destination.
        #
        # Guarded here as well as in the adapter. `RunTracer` says an
        # implementation must not raise, and the Langfuse one does not -- but
        # "must not" is a comment, and the cost of one being wrong is a person
        # losing their answer to a bookkeeping error. The answer has already
        # been written by this point; there is nothing left that failing could
        # usefully abandon.
        try:
            await self._tracer.trace(
                RunTrace(
                    question=question,
                    answer=outcome.answer,
                    record=record,
                    evidence=outcome.evidence,
                )
            )
        except Exception as exc:  # noqa: BLE001 - tracing never costs a reply
            log.warning("reasoning.trace_failed", error=str(exc))
        # `evidence` is carried through: rebuilding the outcome without it
        # would leave the tracer holding nothing on any escalated run.
        return RunOutcome(
            answer=outcome.answer, record=record, evidence=outcome.evidence
        )

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
        if time_question(text):
            # The corpus cannot answer what day it is, and asked to try it
            # abstained -- "I couldn't find anything about that in the
            # messages you can see", which is the grounding rule working
            # correctly on a question that should never have reached it.
            #
            # Answered from the clock with no model call at all: the date is
            # a fact this process holds, and a model asked to repeat it could
            # only get it wrong.
            log.info("reasoning.external_route", route="time")
            return _route(EXTERNAL_ROUTE, "time"), refusal(
                current_time_answer(detect(text), self._clock())
            )
        # Remembered questions are the asker's own words as typed.
        defi = defi_question(text, tuple(turn.question for turn in question.memory.turns))
        if defi is not None:
            # Before the wallet route: "my pools on 0x..." names an address
            # too, and it is positions that were asked about.
            return await self._defi_route(question, defi)
        wallet = wallet_question(text)
        if wallet is not None:
            # A balance is never in the corpus. A channel message about a
            # wallet is a record of what somebody said, and answering "what
            # does 0x... hold" from one is how the assistant reported a
            # colleague's project summary as somebody's balance -- which is
            # exactly what it did before this route existed.
            asked = question
            if wallet.address is None:
                saved = _saved_wallet(question)
                if saved is None:
                    log.info("reasoning.external_route", route="wallet_no_address")
                    return (
                        _route(EXTERNAL_ROUTE, "wallet_address_missing"),
                        refusal(WALLET_ADDRESS_MISSING),
                    )
                # Spelled out for the tool proposal, which sees the question
                # and nothing else: "what's my balance" names no address, so a
                # model reading it alone cannot produce one.
                #
                # This is a hint, not the authorisation. The guard admits the
                # address because it is one of `asker_values` -- the exact
                # strings the store holds for this person -- so a wrong
                # address put here would be refused rather than sent. Same
                # arrangement as the market follow-up above, where the
                # addition is checked by membership rather than trusted.
                asked = replace(question, text=f"{question.text} {saved}")
                log.info("reasoning.external_route", route="wallet_saved_address")
            outcome = await self._loop.run_external(
                asked, CHAIN_SERVERS, verbatim=True
            )
            answer = outcome.answer
            if answer.abstained:
                answer = replace(answer, text=WALLET_UNAVAILABLE)
            log.info("reasoning.external_route", route="wallet")
            return (
                _route(EXTERNAL_ROUTE, "wallet"),
                replace(outcome, answer=answer),
            )
        if explicit_web_search(text):
            log.info("reasoning.external_route", route="explicit_web_search")
            outcome = await self._loop.run_external(question)
            return _route(EXTERNAL_ROUTE, "explicit_web_search"), outcome
        return None


    async def _defi_route(
        self, question: Question, defi: DefiQuestion
    ) -> tuple[Decision, RunOutcome]:
        """Liquidity or lending positions, from the chain, never the corpus.

        Same address rules as the wallet route: the one in the question, or
        the asker's saved wallet (admitted by the guard as an asker fact), or
        a request for one.
        """
        asked = question
        if defi.carried:
            # From the asker's own earlier question, as typed: spelled out so
            # the tool proposal, which sees this question alone, can copy it.
            asked = replace(question, text=f"{question.text} {defi.address}")
            log.info("reasoning.external_route", route="defi_follow_up")
        elif defi.address is None:
            saved = _saved_wallet(question)
            if saved is None:
                log.info("reasoning.external_route", route="defi_no_address")
                return (
                    _route(EXTERNAL_ROUTE, "wallet_address_missing"),
                    refusal(WALLET_ADDRESS_MISSING),
                )
            asked = replace(question, text=f"{question.text} {saved}")
        outcome = await self._loop.run_external(
            asked, DEFI_SERVERS, verbatim=True, tools={DEFI_TOOLS[defi.kind]}
        )
        answer = outcome.answer
        if answer.abstained:
            answer = replace(answer, text=WALLET_UNAVAILABLE)
        log.info("reasoning.external_route", route="defi", kind=str(defi.kind))
        return (
            _route(EXTERNAL_ROUTE, f"defi_{defi.kind}"),
            replace(outcome, answer=answer),
        )

def current_time_answer(language: Language, now: datetime | None = None) -> str:
    """What the date and time are, said in the asker's language.

    UTC and labelled, like every other time this assistant states. A person
    reading a bare time reads it as their own, and this one is not.
    """
    moment = now or utc_now()
    stamp = moment.strftime("%A %d %B %Y, %H:%M")
    if language is Language.PORTUGUESE:
        return f"Agora são **{stamp} UTC**."
    return f"It is **{stamp} UTC**."


def _saved_wallet(question: Question) -> str | None:
    """The asker's own Ethereum address, if they have told the assistant one.

    Only an address: `asker_values` may hold a Bitcoin wallet too, and this
    lookup reads Ethereum and Base. Sending a Bitcoin address to an EVM node
    would return a confident zero.
    """
    from chatmemory.domain.chain import is_address

    for value in sorted(question.asker_values):
        if is_address(value):
            return value
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
    tracer: RunTracer | None = None,
    clock: Clock = utc_now,
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

    `clock` is the one the time route answers from and the default planner
    and synthesizer state in their prompts, so a fixed clock fixes all three.
    """
    critic = ModelCritic(model)
    writer = synthesizer or ModelSynthesizer(model, clock)
    fixed = FixedPath(fixed_driver or CorrectiveDriver(retrieval, critic), writer)
    loop = ReasoningLoop(
        loop_driver or CorrectiveDriver(retrieval, critic, budget=LOOP_BUDGET),
        planner or ModelPlanner(model, clock),
        writer,
        tools=tools,
    )
    return ReasoningAnswerService(fixed, loop, recorder, tracer=tracer, clock=clock)


LOOP_BUDGET = Budget(max_attempts=6, max_model_calls=16, max_tool_calls=24)
"""A whole-run allowance, not a per-sub-question one: a plan with more steps
than attempts stops early and says so, rather than letting the planner
multiply what a run may spend."""
