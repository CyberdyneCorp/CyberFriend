"""Scheduled tasks: what a run may do, and what it must not send.

The two properties worth testing hardest are both about restraint. A run that
finds nothing sends nothing -- and the listing must therefore make a quiet task
distinguishable from a broken one, or silence becomes a bug report. And a run
happens with nobody present, so it can neither approve an action nor see a
channel its owner has since lost.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chatmemory.app.ask import AskOutcome, AskRequest
from chatmemory.app.disclosure import ScopedAnswer
from chatmemory.app.schedules import (
    CreateRefusal,
    ScheduledTaskRunner,
    ScheduleService,
)
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.answers import Answer
from chatmemory.ports.notifications import DeliveryResult
from chatmemory.ports.schedules import DueTask, ScheduledTask, TaskOutcome

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LEO = PersonRef(platform="discord", platform_user_id=7)
BRUNO = PersonRef(platform="discord", platform_user_id=9)


class FakeStore:
    def __init__(self, cap: int = 5) -> None:
        self.tasks: dict[int, tuple[PersonRef, ScheduledTask]] = {}
        self.runs: list[tuple[int, TaskOutcome]] = []
        self.disabled: list[tuple[PersonRef, str]] = []
        self._next_id = 1
        self._cap = cap

    async def create(self, person, question, interval_hours, first_run_at):  # noqa: ANN001
        mine = [t for p, t in self.tasks.values() if p == person]
        if len(mine) >= self._cap:
            return None
        task = ScheduledTask(
            id=self._next_id,
            question=question,
            interval_hours=interval_hours,
            next_run_at=first_run_at,
            created_at=NOW,
        )
        self.tasks[self._next_id] = (person, task)
        self._next_id += 1
        return task

    async def for_person(self, person):  # noqa: ANN001
        return [t for p, t in self.tasks.values() if p == person]

    async def delete(self, person, task_id):  # noqa: ANN001
        held = self.tasks.get(task_id)
        if held is None or held[0] != person:
            return False
        del self.tasks[task_id]
        return True

    async def claim_due(self, now, limit):  # noqa: ANN001
        return [
            DueTask(id=t.id, person=p, question=t.question)
            for p, t in self.tasks.values()
            if t.next_run_at <= now and t.active
        ][:limit]

    async def record_run(self, task_id, outcome, now):  # noqa: ANN001
        self.runs.append((task_id, outcome))

    async def disable(self, person, reason, now):  # noqa: ANN001
        self.disabled.append((person, reason))
        return 1


class FakeAsks:
    """Records how a run was asked, which is where the safety properties are."""

    def __init__(self, answer: Answer | None = None, explode: bool = False) -> None:
        self._answer = answer
        self._explode = explode
        self.calls: list[tuple[AskRequest, object, bool]] = []

    async def ask(self, request, confirm=None, *, metered=True):  # noqa: ANN001
        self.calls.append((request, confirm, metered))
        if self._explode:
            raise RuntimeError("the model is down")
        if self._answer is None:
            return AskOutcome(None)
        return AskOutcome(ScopedAnswer(self._answer, frozenset()))


class FakeMessenger:
    def __init__(self, result: DeliveryResult = DeliveryResult.SENT) -> None:
        self.result = result
        self.sent: list[tuple[PersonRef, str]] = []

    async def deliver(self, person, task_id, text):  # noqa: ANN001
        if self.result is DeliveryResult.SENT:
            self.sent.append((person, text))
        return self.result


def runner(store: FakeStore, asks: FakeAsks, messenger: FakeMessenger) -> ScheduledTaskRunner:
    return ScheduledTaskRunner(store, asks, messenger)  # type: ignore[arg-type]


# --- creating -----------------------------------------------------------


async def test_a_task_is_created_and_first_runs_one_interval_later() -> None:
    """Not immediately: somebody setting up a daily digest expects it
    tomorrow, and running on create would make create-and-delete a trigger."""
    store = FakeStore()
    result = await ScheduleService(store).create(LEO, "what did I miss", 24, now=NOW)  # type: ignore[arg-type]

    assert result.created
    assert result.task is not None
    assert result.task.next_run_at == NOW + timedelta(hours=24)


@pytest.mark.parametrize("hours", [0, -1, 25, 100])
async def test_an_out_of_range_interval_is_refused(hours: int) -> None:
    result = await ScheduleService(FakeStore()).create(LEO, "x", hours, now=NOW)  # type: ignore[arg-type]
    assert result.refusal is CreateRefusal.INTERVAL_OUT_OF_RANGE


@pytest.mark.parametrize("hours", [1, 12, 24])
async def test_the_bounds_themselves_are_allowed(hours: int) -> None:
    result = await ScheduleService(FakeStore()).create(LEO, "x", hours, now=NOW)  # type: ignore[arg-type]
    assert result.created


async def test_an_empty_question_is_refused() -> None:
    result = await ScheduleService(FakeStore()).create(LEO, "   ", 6, now=NOW)  # type: ignore[arg-type]
    assert result.refusal is CreateRefusal.EMPTY_QUESTION


async def test_a_person_cannot_exceed_their_cap() -> None:
    store = FakeStore(cap=2)
    service = ScheduleService(store, tasks_per_person=2)  # type: ignore[arg-type]
    for _ in range(2):
        assert (await service.create(LEO, "x", 6, now=NOW)).created
    assert (await service.create(LEO, "x", 6, now=NOW)).refusal is CreateRefusal.AT_CAP


async def test_one_persons_cap_is_not_anothers() -> None:
    store = FakeStore(cap=1)
    service = ScheduleService(store, tasks_per_person=1)  # type: ignore[arg-type]
    assert (await service.create(LEO, "x", 6, now=NOW)).created
    assert (await service.create(BRUNO, "x", 6, now=NOW)).created


# --- listing and deleting are per person --------------------------------


async def test_a_person_sees_only_their_own_tasks() -> None:
    store = FakeStore()
    service = ScheduleService(store)  # type: ignore[arg-type]
    await service.create(LEO, "mine", 6, now=NOW)
    await service.create(BRUNO, "theirs", 6, now=NOW)

    assert [t.question for t in await service.list_for(LEO)] == ["mine"]


async def test_deleting_someone_elses_task_does_nothing() -> None:
    """And the caller cannot tell it from a task that does not exist."""
    store = FakeStore()
    service = ScheduleService(store)  # type: ignore[arg-type]
    created = await service.create(BRUNO, "theirs", 6, now=NOW)
    assert created.task is not None

    assert await service.delete(LEO, created.task.id) is False
    assert await service.delete(LEO, 9999) is False
    assert len(await service.list_for(BRUNO)) == 1


async def test_deleting_your_own_task_removes_it() -> None:
    store = FakeStore()
    service = ScheduleService(store)  # type: ignore[arg-type]
    created = await service.create(LEO, "mine", 6, now=NOW)
    assert created.task is not None

    assert await service.delete(LEO, created.task.id) is True
    assert await service.list_for(LEO) == []


# --- what a run is ------------------------------------------------------


async def test_a_run_asks_the_owners_question_as_the_owner() -> None:
    store = FakeStore()
    await ScheduleService(store).create(LEO, "what did I miss", 6, now=NOW - timedelta(hours=7))  # type: ignore[arg-type]
    asks = FakeAsks(Answer(text="three things happened"))
    messenger = FakeMessenger()

    sent = await runner(store, asks, messenger).run_due(NOW)

    assert sent == 1
    request, confirm, metered = asks.calls[0]
    assert request.asker == LEO
    assert request.text == "what did I miss"
    # A direct message: the answer is composed for an audience of one, which
    # is where it is going.
    assert request.destination is None
    # Nobody is present, so nothing may be approved.
    assert confirm is None
    # And it is not the person's own interactive traffic.
    assert metered is False


async def test_a_run_delivers_the_answer_to_its_owner() -> None:
    store = FakeStore()
    await ScheduleService(store).create(LEO, "q", 6, now=NOW - timedelta(hours=7))  # type: ignore[arg-type]
    messenger = FakeMessenger()

    await runner(store, FakeAsks(Answer(text="here it is")), messenger).run_due(NOW)

    assert messenger.sent == [(LEO, "here it is")]
    assert store.runs == [(1, TaskOutcome.REPORTED)]


async def test_a_task_that_is_not_due_does_not_run() -> None:
    store = FakeStore()
    await ScheduleService(store).create(LEO, "q", 6, now=NOW)  # type: ignore[arg-type]
    asks = FakeAsks(Answer(text="x"))

    assert await runner(store, asks, FakeMessenger()).run_due(NOW) == 0
    assert asks.calls == []


# --- silence ------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        Answer(text="I found nothing", abstained=True),
        Answer(text="   "),
        None,
    ],
)
async def test_a_run_with_nothing_to_say_sends_nothing(answer: Answer | None) -> None:
    """An hourly "I found nothing" is what makes somebody mute the bot -- and
    muting it also silences the obligation notifications they do need."""
    store = FakeStore()
    await ScheduleService(store).create(LEO, "q", 1, now=NOW - timedelta(hours=2))  # type: ignore[arg-type]
    messenger = FakeMessenger()

    sent = await runner(store, FakeAsks(answer), messenger).run_due(NOW)

    assert sent == 0
    assert messenger.sent == []
    # Recorded, so the owner can tell this from a task that never ran.
    assert store.runs == [(1, TaskOutcome.NOTHING)]


async def test_a_failing_run_sends_nothing_and_is_recorded() -> None:
    store = FakeStore()
    await ScheduleService(store).create(LEO, "q", 1, now=NOW - timedelta(hours=2))  # type: ignore[arg-type]
    messenger = FakeMessenger()

    sent = await runner(store, FakeAsks(explode=True), messenger).run_due(NOW)

    assert sent == 0
    assert messenger.sent == []
    assert store.runs == [(1, TaskOutcome.FAILED)]


async def test_one_failing_task_does_not_stop_the_sweep() -> None:
    store = FakeStore()
    service = ScheduleService(store)  # type: ignore[arg-type]
    await service.create(LEO, "first", 1, now=NOW - timedelta(hours=2))
    await service.create(BRUNO, "second", 1, now=NOW - timedelta(hours=2))

    await runner(store, FakeAsks(explode=True), FakeMessenger()).run_due(NOW)

    assert len(store.runs) == 2, "both tasks were attempted"


# --- closed direct messages ---------------------------------------------


async def test_closed_direct_messages_stop_every_task_of_that_person() -> None:
    """The obstacle is their settings, not this question, so retrying the rest
    would be knocking on a door already shut."""
    store = FakeStore()
    await ScheduleService(store).create(LEO, "q", 1, now=NOW - timedelta(hours=2))  # type: ignore[arg-type]
    messenger = FakeMessenger(DeliveryResult.CLOSED)

    sent = await runner(store, FakeAsks(Answer(text="something")), messenger).run_due(NOW)

    assert sent == 0
    assert store.disabled and store.disabled[0][0] == LEO
    assert store.runs == [(1, TaskOutcome.CLOSED)]


async def test_a_transient_send_failure_is_a_failed_run_and_stops_nothing() -> None:
    store = FakeStore()
    await ScheduleService(store).create(LEO, "q", 1, now=NOW - timedelta(hours=2))  # type: ignore[arg-type]
    messenger = FakeMessenger(DeliveryResult.FAILED)

    sent = await runner(store, FakeAsks(Answer(text="something")), messenger).run_due(NOW)

    assert sent == 0
    assert store.disabled == []
    assert store.runs == [(1, TaskOutcome.FAILED)]


# --- reached from the process that runs ---------------------------------


def _function(path, name):  # noqa: ANN001, ANN202
    import ast

    found = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    ]
    assert found, f"{path.name} has no function {name}"
    return found[0]


def _calls(scope, func):  # noqa: ANN001, ANN202
    import ast

    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == func
    ]


def test_the_bot_builds_and_attaches_both_halves() -> None:
    """Commands with no runner create tasks nothing performs -- silently,
    which is also what this feature looks like when it is working."""
    from pathlib import Path

    bot = Path(__file__).resolve().parents[2] / "src" / "chatmemory" / "entrypoints" / "bot.py"
    build = _function(bot, "build_bot")
    assert _calls(build, "build_schedules"), "the commands need a service"
    assert _calls(build, "attach_schedules"), "built but not attached is not wired"
    assert _calls(build, "build_task_runner"), "without this nothing ever runs a task"


def test_the_sweep_is_started_beside_the_gateway() -> None:
    from pathlib import Path

    bot = Path(__file__).resolve().parents[2] / "src" / "chatmemory" / "entrypoints" / "bot.py"
    main = _function(bot, "main")
    assert _calls(main, "scheduled_task_loop"), (
        "a runner nothing starts is a queue that never drains"
    )


def test_the_sweep_waits_for_the_gateway() -> None:
    """Before the connection identifies, every person resolves to nothing
    readable -- and this feature renders "nothing" as silence, so a pass in
    that state would advance every schedule and tell nobody."""
    from pathlib import Path

    bot = Path(__file__).resolve().parents[2] / "src" / "chatmemory" / "entrypoints" / "bot.py"
    source = bot.read_text()
    start = source.index("async def scheduled_task_loop")
    body = source[start : source.index("async def notification_loop", start)]
    assert "await ready.wait()" in body


def test_an_operator_can_switch_it_off() -> None:
    from chatmemory.composition import build_schedules
    from chatmemory.config import Settings

    base = {
        "discord_token": "x",
        "discord_guild_id": 1,
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "llm_api_key": "k",
    }
    assert build_schedules(Settings(**base), object()) is None  # type: ignore[arg-type]
    enabled = Settings(**(base | {"scheduled_tasks_enabled": True}))  # type: ignore[arg-type]
    assert build_schedules(enabled, object()) is not None  # type: ignore[arg-type]
