"""The two-tier answer engine.

A fixed corrective path carries the routine traffic; a reasoning loop handles
the minority of questions with separable sub-goals. Both produce the same
contract, both retrieve only through a viewer-scoped tool, and both are
stopped by the same driver-held budget.

The pieces worth knowing about:

*   `verdicts` / `policy` -- an enumerated critic verdict, and a pure function
    from verdict to corrective action. Testable with no model present.
*   `budgets` -- limits the driver enforces, which no configuration can widen.
*   `gate` -- the cheap relevance signal that decides whether to pay for the
    expensive evaluation, and refuses to run uncalibrated.
*   `contract` -- the shared answer and the operator record, including
    provenance that is validated rather than asserted.
"""

from __future__ import annotations

from chatmemory.app.reasoning.budgets import Budget, BudgetLedger, Spend
from chatmemory.app.reasoning.contract import (
    AnswerPath,
    Decision,
    DecisionMaker,
    RunOutcome,
    RunRecord,
    RunStatus,
    TerminalCause,
)
from chatmemory.app.reasoning.errors import ConfigurationError, RetrievalUnavailable
from chatmemory.app.reasoning.evidence import Evidence, EvidenceLedger
from chatmemory.app.reasoning.fixed import CorrectiveDriver, FixedPath
from chatmemory.app.reasoning.gate import Calibration, RelevanceGate
from chatmemory.app.reasoning.loop import ReasoningLoop
from chatmemory.app.reasoning.policy import Action, CorrectivePolicy, PolicyConfig, action_for
from chatmemory.app.reasoning.ports import RetrievalResult, RetrievalTool
from chatmemory.app.reasoning.service import ReasoningAnswerService, build_answer_service
from chatmemory.app.reasoning.verdicts import Assessment, Verdict

__all__ = [
    "Action",
    "AnswerPath",
    "Assessment",
    "Budget",
    "BudgetLedger",
    "Calibration",
    "ConfigurationError",
    "CorrectiveDriver",
    "CorrectivePolicy",
    "Decision",
    "DecisionMaker",
    "Evidence",
    "EvidenceLedger",
    "FixedPath",
    "PolicyConfig",
    "ReasoningAnswerService",
    "ReasoningLoop",
    "RelevanceGate",
    "RetrievalResult",
    "RetrievalTool",
    "RetrievalUnavailable",
    "RunOutcome",
    "RunRecord",
    "RunStatus",
    "Spend",
    "TerminalCause",
    "Verdict",
    "action_for",
    "build_answer_service",
]
