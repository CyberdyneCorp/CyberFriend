"""Scheduled tasks: what a person may set up, and what running one means.

Two halves with different concerns.

`ScheduleService` is policy a person meets at a command: the interval bounds,
the per-person cap, and the rule that you only ever see or delete your own.
Everything it refuses, it refuses the same way whether the task belongs to
somebody else or does not exist -- a reply that could tell those apart is a way
to learn that another person's task exists.

`ScheduledTaskRunner` is what a due task actually does, and its whole design is
that it does almost nothing itself. It asks `AskService` the person's own
question, as them, with no confirmation surface. Two properties follow from
that and neither is new code:

*   **Access is resolved now.** The ACL resolver reads live guild state for the
    owner at the moment of the run, so a task created when they could read a
    channel stops drawing on it the moment they cannot.
*   **Nothing can act.** There is nobody to show a prompt to, so every
    state-changing tool is refused for want of a confirmation -- which is what
    `AskService._attending` already does when nothing attends.

Silence is the other decision. A run that abstains sends nothing: an hourly
"I found nothing" is what makes somebody mute the assistant, and muting it also
silences the obligation notifications they do need. The cost is that a quiet
task and a broken one look identical, which is why every run records its
outcome and the listing shows it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

import structlog

from chatmemory.app.ask import AskRequest, AskService
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.notifications import DeliveryResult
from chatmemory.ports.schedules import (
    MAX_INTERVAL_HOURS,
    MIN_INTERVAL_HOURS,
    DueTask,
    ScheduledTask,
    ScheduleStore,
    TaskOutcome,
)

log = structlog.get_logger()

DEFAULT_TASKS_PER_PERSON = 5
DEFAULT_BATCH = 20


class CreateRefusal(StrEnum):
    """Why a task was not created. Each maps to a sentence the person is shown."""

    INTERVAL_OUT_OF_RANGE = "interval_out_of_range"
    EMPTY_QUESTION = "empty_question"
    AT_CAP = "at_cap"
    UNKNOWN_PERSON = "unknown_person"


@dataclass(frozen=True, slots=True)
class CreateResult:
    task: ScheduledTask | None = None
    refusal: CreateRefusal | None = None

    @property
    def created(self) -> bool:
        return self.task is not None


class TaskMessenger(Protocol):
    """Sends one finished answer to the person who scheduled it."""

    async def deliver(self, person: PersonRef, task_id: int, text: str) -> DeliveryResult:
        """SENT when it arrived, CLOSED when the person refuses direct messages
        (or is gone), FAILED for anything transient. Only CLOSED stops work."""
        ...


class ScheduleService:
    """Creating, listing and deleting a person's own scheduled tasks."""

    def __init__(
        self, store: ScheduleStore, tasks_per_person: int = DEFAULT_TASKS_PER_PERSON
    ) -> None:
        self._store = store
        self._cap = tasks_per_person

    async def create(
        self, person: PersonRef, question: str, interval_hours: int, now: datetime | None = None
    ) -> CreateResult:
        if not question.strip():
            return CreateResult(refusal=CreateRefusal.EMPTY_QUESTION)
        if not MIN_INTERVAL_HOURS <= interval_hours <= MAX_INTERVAL_HOURS:
            return CreateResult(refusal=CreateRefusal.INTERVAL_OUT_OF_RANGE)
        # The first run is one interval away, not immediate. Somebody setting
        # up a daily digest at 2pm expects it tomorrow, and running on create
        # would also let a person trigger runs by creating and deleting.
        moment = now or datetime.now(UTC)
        first = moment + timedelta(hours=interval_hours)
        task = await self._store.create(person, question.strip(), interval_hours, first)
        if task is None:
            # The store refuses both at the cap and for a person it has never
            # seen, and the two are distinguishable there but not worth
            # distinguishing here: both mean "no task was made".
            return CreateResult(refusal=CreateRefusal.AT_CAP)
        return CreateResult(task=task)

    async def list_for(self, person: PersonRef) -> Sequence[ScheduledTask]:
        return await self._store.for_person(person)

    async def delete(self, person: PersonRef, task_id: int) -> bool:
        return await self._store.delete(person, task_id)


class ScheduledTaskRunner:
    """Runs due tasks and delivers what they produced."""

    def __init__(
        self,
        store: ScheduleStore,
        asks: AskService,
        messenger: TaskMessenger,
        batch: int = DEFAULT_BATCH,
    ) -> None:
        self._store = store
        self._asks = asks
        self._messenger = messenger
        self._batch = batch

    async def run_due(self, now: datetime | None = None) -> int:
        """Run every task due now. Returns how many produced a message."""
        moment = now or datetime.now(UTC)
        due = await self._store.claim_due(moment, self._batch)
        sent = 0
        for task in due:
            if await self._run_one(task, moment):
                sent += 1
        return sent

    async def _run_one(self, task: DueTask, now: datetime) -> bool:
        try:
            outcome = await self._asks.ask(
                AskRequest(
                    asker=task.person,
                    text=task.question,
                    # None means a direct message: the answer is composed for
                    # an audience of one, which is where it is going.
                    destination=None,
                    location_id=task.person.platform_user_id,
                ),
                # Nobody is present, so nothing may be approved and every
                # state-changing tool is refused. See the module docstring.
                None,
                # Not the person's own interactive traffic; see `AskService.ask`.
                metered=False,
            )
        except Exception as exc:  # noqa: BLE001 - one task must not stop the sweep
            log.warning("schedules.run_failed", task_id=task.id, error=str(exc))
            await self._store.record_run(task.id, TaskOutcome.FAILED, now)
            return False

        scoped = outcome.scoped
        # An alert proposal is a prompt with buttons for somebody present; as a
        # text in a direct message nobody could confirm it, so it is not sent.
        proposal = outcome.alert is not None
        if proposal or scoped is None or scoped.answer.abstained or not scoped.answer.text.strip():
            # Silence by design. Recorded so the owner can tell a task that
            # ran and found nothing from one that has not run.
            await self._store.record_run(task.id, TaskOutcome.NOTHING, now)
            return False

        result = await self._messenger.deliver(task.person, task.id, scoped.answer.text)
        if result is DeliveryResult.FAILED:
            # A Discord blip, not a refusal: this run is lost, the task stays.
            await self._store.record_run(task.id, TaskOutcome.FAILED, now)
            return False
        if result is DeliveryResult.CLOSED:
            # Their direct messages are closed. Stopping every task of theirs
            # rather than this one: the obstacle is the person's settings, not
            # this question, and retrying the rest would be the assistant
            # knocking on a door it has been told is shut.
            stopped = await self._store.disable(
                task.person, "direct messages are closed", now
            )
            await self._store.record_run(task.id, TaskOutcome.CLOSED, now)
            log.info("schedules.disabled", person=str(task.person), tasks=stopped)
            return False

        await self._store.record_run(task.id, TaskOutcome.REPORTED, now)
        return True
