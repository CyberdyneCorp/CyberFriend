"""An abstaining fixed run escalates to the loop that can reach external tools."""

from __future__ import annotations

from chatmemory.app.reasoning.contract import (
    AnswerPath,
    RunOutcome,
    RunRecord,
    RunStatus,
    TerminalCause,
)
from chatmemory.app.reasoning.service import ReasoningAnswerService
from chatmemory.app.routing import Route, RoutingDecision
from chatmemory.domain.audience import private_audience
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer, Question

ASKER = PersonRef("discord", 1)
GENERAL = ChannelRef("discord", 10)


class StubPath:
    """A path that returns a fixed status and counts how often it ran."""

    def __init__(self, status: RunStatus, *, can_reach_outside: bool = False) -> None:
        self.status = status
        self.can_reach_outside = can_reach_outside
        self.runs = 0

    async def run(self, question: Question) -> RunOutcome:
        self.runs += 1
        return RunOutcome(
            answer=Answer("stub"),
            record=RunRecord(
                path=AnswerPath.FIXED,
                status=self.status,
                cause=TerminalCause.EVIDENCE_SUFFICIENT,
            ),
        )


def always_fixed(_: str) -> RoutingDecision:
    return RoutingDecision(Route.FIXED, frozenset())


def question(text: str) -> Question:
    visible = frozenset({GENERAL})
    return Question(
        text=text, asker=Viewer(ASKER, visible), audience=private_audience(ASKER, visible)
    )


async def test_an_abstaining_fixed_run_escalates_to_the_tool_capable_loop() -> None:
    """The fixed path carries nearly every question and searches only the corpus.

    Without escalation every external tool was unreachable for exactly the
    questions it exists for: "who wrote the novel Dune" abstained while a
    registered, routed, authorised Wikipedia tool sat unused.
    """
    fixed = StubPath(RunStatus.ABSTAINED)
    loop = StubPath(RunStatus.ANSWERED, can_reach_outside=True)
    service = ReasoningAnswerService(fixed, loop, classifier=always_fixed)  # type: ignore[arg-type]

    outcome = await service.answer_run(question("who wrote the novel Dune"))

    assert loop.runs == 1
    assert outcome.record.status is RunStatus.ANSWERED
    assert any(
        d.outcome == "fixed_abstained_trying_external_tools" for d in outcome.record.decisions
    )


async def test_a_corpus_answer_is_never_second_guessed_by_the_web() -> None:
    """The team's own conversations always win when they have an answer."""
    fixed = StubPath(RunStatus.ANSWERED)
    loop = StubPath(RunStatus.ANSWERED, can_reach_outside=True)
    service = ReasoningAnswerService(fixed, loop, classifier=always_fixed)  # type: ignore[arg-type]

    await service.answer_run(question("when is the espresso machine repair"))

    assert loop.runs == 0


async def test_no_escalation_without_external_tools() -> None:
    fixed = StubPath(RunStatus.ABSTAINED)
    loop = StubPath(RunStatus.ANSWERED, can_reach_outside=False)
    service = ReasoningAnswerService(fixed, loop, classifier=always_fixed)  # type: ignore[arg-type]

    await service.answer_run(question("anything"))

    assert loop.runs == 0


async def test_an_escalation_that_also_abstains_keeps_the_original() -> None:
    """An escalation that finds nothing says nothing the first run did not."""
    fixed = StubPath(RunStatus.ABSTAINED)
    loop = StubPath(RunStatus.ABSTAINED, can_reach_outside=True)
    service = ReasoningAnswerService(fixed, loop, classifier=always_fixed)  # type: ignore[arg-type]

    outcome = await service.answer_run(question("anything"))

    assert loop.runs == 1
    assert outcome.record.status is RunStatus.ABSTAINED
