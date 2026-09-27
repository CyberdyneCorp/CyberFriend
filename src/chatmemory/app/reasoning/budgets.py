"""Run limits, enforced by the driver and by nothing else.

Two properties matter here and neither is negotiable:

*   The budget is held by the component that drives the run, not by the
    corrective policy. A policy that is misconfigured -- or that a future
    change makes configurable in some new way -- must not be able to widen
    its own bound. `CorrectivePolicy` is never handed a ledger.
*   Any recursion limit underneath is *derived* from `max_attempts`. A
    hardcoded framework limit silently caps a raised application limit and
    surfaces as a recursion error rather than as the extra attempts that
    were asked for.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

# Each attempt costs at most one retrieval, one evaluation and one synthesis
# step, plus a margin for the frame the driver itself occupies.
FRAMES_PER_ATTEMPT = 3
RECURSION_MARGIN = 10


class ExhaustedBy(StrEnum):
    ATTEMPTS = "attempts"
    MODEL_CALLS = "model_calls"
    TOOL_CALLS = "tool_calls"
    PROMPT_TOKENS = "prompt_tokens"
    WALL_CLOCK = "wall_clock"


@dataclass(frozen=True, slots=True)
class Budget:
    """Server-side limits. Never sourced from a model or from a question."""

    max_attempts: int = 3
    max_model_calls: int = 8
    max_tool_calls: int = 12
    max_prompt_tokens: int = 60_000
    max_seconds: float = 45.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

    @property
    def recursion_limit(self) -> int:
        """Derived, so raising `max_attempts` raises this too."""
        return self.max_attempts * FRAMES_PER_ATTEMPT + RECURSION_MARGIN


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """One model call: which stage asked, which model answered, what it cost.

    Carried so a trace can hold one generation per call, from which the trace
    store prices the run. Counts and names only -- never a prompt or a reply.
    """

    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True, slots=True)
class ToolUsage:
    """One federated tool call: its name, how it ended and how long it took.

    Deliberately without arguments: they can hold a wallet address or a
    query, and nothing downstream of the run needs them.
    """

    name: str
    outcome: str
    failed: bool
    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True, slots=True)
class Spend:
    """What a run consumed. Reported on every outcome, including failures."""

    attempts: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    elapsed_seconds: float = 0.0
    completion_tokens: int = 0
    models: tuple[ModelUsage, ...] = ()
    tools: tuple[ToolUsage, ...] = ()


class BudgetLedger:
    """Mutable spend against a fixed budget, owned by the driver.

    The clock is injected so a wall-clock test does not have to sleep. The
    wall clock only stamps usage records; limits run on the monotonic one.
    """

    def __init__(
        self,
        budget: Budget,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = utc_now,
    ) -> None:
        self._budget = budget
        self._clock = clock
        self._wall = wall
        self._started = clock()
        self._attempts = 0
        self._model_calls = 0
        self._tool_calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._models: list[ModelUsage] = []
        self._tools: list[ToolUsage] = []

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self._started

    def exhausted(self) -> ExhaustedBy | None:
        """The first bound that has been reached, or None."""
        if self._attempts >= self._budget.max_attempts:
            return ExhaustedBy.ATTEMPTS
        if self._model_calls >= self._budget.max_model_calls:
            return ExhaustedBy.MODEL_CALLS
        if self._tool_calls >= self._budget.max_tool_calls:
            return ExhaustedBy.TOOL_CALLS
        if self._prompt_tokens >= self._budget.max_prompt_tokens:
            return ExhaustedBy.PROMPT_TOKENS
        if self.elapsed_seconds >= self._budget.max_seconds:
            return ExhaustedBy.WALL_CLOCK
        return None

    def begin_attempt(self) -> ExhaustedBy | None:
        """Claim one attempt, or report which bound refused it.

        The check happens *before* the attempt is counted, so a run with
        `max_attempts=1` performs exactly one attempt.
        """
        reached = self.exhausted()
        if reached is not None:
            return reached
        self._attempts += 1
        return None

    def now(self) -> datetime:
        """Wall-clock time, for stamping the start of a call before it is made."""
        return self._wall()

    def charge_model_call(
        self,
        prompt_tokens: int = 0,
        calls: int = 1,
        *,
        stage: str = "",
        model: str = "",
        completion_tokens: int = 0,
        started_at: datetime | None = None,
    ) -> None:
        """Count a model call and, when one was made, record its usage.

        `calls=0` is a stage that answered without a model (a heuristic): it
        is charged nothing and leaves no usage record.
        """
        self._model_calls += calls
        self._prompt_tokens += prompt_tokens
        self._completion_tokens += completion_tokens
        if calls < 1:
            return
        ended = self._wall()
        self._models.append(
            ModelUsage(
                stage=stage,
                model=model,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                started_at=started_at or ended,
                ended_at=ended,
            )
        )

    def charge_tool_call(self, calls: int = 1) -> None:
        self._tool_calls += calls

    def record_tool(
        self, name: str, outcome: str, *, failed: bool, started_at: datetime
    ) -> None:
        """Record how a federated tool call ended. Its charge is taken separately."""
        self._tools.append(
            ToolUsage(
                name=name,
                outcome=outcome,
                failed=failed,
                started_at=started_at,
                ended_at=self._wall(),
            )
        )

    def spend(self) -> Spend:
        return Spend(
            attempts=self._attempts,
            model_calls=self._model_calls,
            tool_calls=self._tool_calls,
            prompt_tokens=self._prompt_tokens,
            elapsed_seconds=self.elapsed_seconds,
            completion_tokens=self._completion_tokens,
            models=tuple(self._models),
            tools=tuple(self._tools),
        )
