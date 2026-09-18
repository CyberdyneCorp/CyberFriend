"""Storing what a person asked to have asked on their behalf.

The store is where the bounds live that a command must not be the only thing
enforcing: a task belongs to exactly one person, an interval is between one
hour and twenty-four, and a due task is claimed once however many sweeps are
running.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from chatmemory.domain.identity import PersonRef

MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 24


class TaskOutcome(StrEnum):
    """What a run did, as the owner is shown it."""

    REPORTED = "reported"
    NOTHING = "nothing"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    """One task, as its owner sees it."""

    id: int
    question: str
    interval_hours: int
    next_run_at: datetime
    created_at: datetime
    last_run_at: datetime | None = None
    last_outcome: TaskOutcome | None = None
    disabled_at: datetime | None = None
    disabled_reason: str = ""

    @property
    def active(self) -> bool:
        return self.disabled_at is None


@dataclass(frozen=True, slots=True)
class DueTask:
    """A claimed task, with whose it is.

    Carries the owner's platform id because the run is performed as them: the
    answer is scoped to what *they* may read, and delivered to them.
    """

    id: int
    person: PersonRef
    question: str


class ScheduleStore(Protocol):
    async def create(
        self, person: PersonRef, question: str, interval_hours: int, first_run_at: datetime
    ) -> ScheduledTask | None:
        """Store a task, or None when the person is already at their cap."""
        ...

    async def for_person(self, person: PersonRef) -> Sequence[ScheduledTask]:
        """Their own tasks, newest first. Never anybody else's."""
        ...

    async def delete(self, person: PersonRef, task_id: int) -> bool:
        """Delete one of their tasks; False when it is not theirs or not there.

        One answer for both, deliberately: a caller that could tell them apart
        could use this to learn that somebody else's task exists.
        """
        ...

    async def claim_due(self, now: datetime, limit: int) -> Sequence[DueTask]:
        """Claim tasks due at `now`, advancing each schedule before it runs."""
        ...

    async def record_run(self, task_id: int, outcome: TaskOutcome, now: datetime) -> None:
        """Record what a run did."""
        ...

    async def disable(self, person: PersonRef, reason: str, now: datetime) -> int:
        """Stop every task of a person who cannot be messaged. Returns how many."""
        ...
