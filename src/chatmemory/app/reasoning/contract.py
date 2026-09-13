"""One answer contract, produced identically by both paths.

`Answer` (the port) is what the Discord surface sees; `RunRecord` is what an
operator sees. Splitting them is what lets the two terminal outcomes that
must be indistinguishable to a requester -- "the corpus holds nothing" and
"the relevant content was not yours to read" -- stay fully distinguishable to
an operator. The requester-facing text for both is the same constant, and
neither path may reach it by a shorter route than the other.

Provenance is validated rather than described: a decision attributed to a
model with no model call, or to a cheap signal with one, is rejected at
construction. Provenance fields that lie are worse than no provenance,
because they mislead exactly when they are relied upon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import structlog

from chatmemory.app.reasoning.budgets import Spend
from chatmemory.app.reasoning.policy import BlockedAction
from chatmemory.ports.answers import Answer

log = structlog.get_logger()

NOTHING_FOUND = (
    "I couldn't find anything about that in the messages you can see."
)
"""The single wording for every run that ends without an answer.

One wording, whatever the cause. Two would turn the reply into an oracle for
the existence of content the requester may not read.
"""


class AnswerPath(StrEnum):
    FIXED = "fixed"
    LOOP = "loop"


class RunStatus(StrEnum):
    """Answered and abstained are both successes. Failure is a third thing."""

    ANSWERED = "answered"
    ABSTAINED = "abstained"
    FAILED = "failed"

    @property
    def successful(self) -> bool:
        """Abstention is a successful outcome.

        Scoring "I found nothing" as failure is what tunes a system into
        confabulating: the fastest way to stop failing is to make something
        up.
        """
        return self is not RunStatus.FAILED


class TerminalCause(StrEnum):
    """Why the run ended, for operators only."""

    EVIDENCE_SUFFICIENT = "evidence_sufficient"
    CORPUS_EMPTY = "corpus_empty"
    ACCESS_BLOCKED = "access_blocked"
    CONFIGURATION_BLOCKED = "configuration_blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_PROGRESS = "no_progress"
    DEPENDENCY_FAILED = "dependency_failed"


class DecisionMaker(StrEnum):
    """What determined a decision."""

    MODEL = "model"
    RELEVANCE_GATE = "relevance_gate"
    HEURISTIC = "heuristic"
    EMPTY_RESULT = "empty_result"
    POLICY = "policy"
    DRIVER = "driver"
    CLASSIFIER = "classifier"

    @property
    def is_model(self) -> bool:
        return self is DecisionMaker.MODEL


@dataclass(frozen=True, slots=True)
class Decision:
    """One significant decision, and what it cost to reach."""

    name: str
    outcome: str
    made_by: DecisionMaker
    model_calls: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        if self.made_by.is_model and self.model_calls < 1:
            raise ValueError(
                f"decision {self.name!r} is attributed to a model but records "
                f"{self.model_calls} model calls"
            )
        if not self.made_by.is_model and self.model_calls:
            raise ValueError(
                f"decision {self.name!r} was made by {self.made_by} yet records "
                f"{self.model_calls} model calls; a cheap signal costs none"
            )


@dataclass(frozen=True, slots=True)
class RunRecord:
    """The operator-facing account of a run."""

    path: AnswerPath
    status: RunStatus
    cause: TerminalCause
    spend: Spend = field(default_factory=Spend)
    decisions: tuple[Decision, ...] = ()
    queries: tuple[str, ...] = ()
    sub_questions: tuple[str, ...] = ()
    evidence_window_ids: tuple[int, ...] = ()
    blocked_actions: tuple[BlockedAction, ...] = ()

    @property
    def model_calls(self) -> int:
        return sum(d.model_calls for d in self.decisions)

    def decisions_named(self, name: str) -> tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.name == name)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What every path returns: the answer, and the record behind it."""

    answer: Answer
    record: RunRecord


class RunRecorder(Protocol):
    def record(self, run: RunRecord) -> None: ...


class LoggingRunRecorder:
    """Default sink. Structured, so the cause is queryable, and never replied."""

    def record(self, run: RunRecord) -> None:
        log.info(
            "reasoning.run",
            path=str(run.path),
            status=str(run.status),
            cause=str(run.cause),
            attempts=run.spend.attempts,
            model_calls=run.spend.model_calls,
            tool_calls=run.spend.tool_calls,
            prompt_tokens=run.spend.prompt_tokens,
            elapsed_seconds=round(run.spend.elapsed_seconds, 3),
            evidence=len(run.evidence_window_ids),
            blocked=[str(b.action) for b in run.blocked_actions],
        )


def abstention_answer() -> Answer:
    """The one abstention answer, whatever ended the run."""
    return Answer(text=NOTHING_FOUND, abstained=True)


DEPENDENCY_FAILED_TEXT = (
    "I couldn't search just now - the message index didn't answer. "
    "Try again in a moment."
)
"""Said only when retrieval itself failed.

Distinct from abstention on purpose: rendering a dependency failure as "I
found nothing" reports an absence of activity that was never established.
"""


def failure_answer() -> Answer:
    return Answer(text=DEPENDENCY_FAILED_TEXT, abstained=False)
