"""Notifications, reached from the two processes that actually run.

This project's recurring failure is a finished module wired to nothing, and
this feature is the one where that failure is invisible: a queue nobody drains
looks exactly like a server where nobody has asked anybody for anything.

So the chain is read from both entrypoints down, because the feature spans
two processes that share no memory:

  ingest  main -> notification_sweep_loop -> build_obligation_notifier
          -> PostgresNotificationQueue, writing rows

  bot     main -> build_bot(notifications=stack.engine) -> build_delivery
          -> NotificationDelivery over a DiscordAclResolver and a
          DiscordNotificationSender, and main -> notification_loop, draining
          them beside the gateway

The queue table is the seam. Neither half is checked for "was it built" but
for "is it started by the process", which is the distinction eleven merged
features here have failed to make.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chatmemory.adapters.store.notify_postgres import PostgresNotificationQueue
from chatmemory.app.notifications import (
    NotificationDelivery,
    NotificationPreferences,
    ObligationNotifier,
)
from chatmemory.config import Settings
from chatmemory.ports.notifications import NotificationQueue

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"
BOT = SRC / "entrypoints" / "bot.py"
INGEST = SRC / "entrypoints" / "ingest.py"
CLIENT = SRC / "adapters" / "discord" / "bot.py"

BASE = {
    "discord_token": "x",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "llm_api_key": "k",
}


def _function(path: Path, name: str) -> ast.AST:
    found = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    ]
    assert found, f"{path.name} has no function {name}"
    return found[0]


def _calls(scope: ast.AST, func: str, keyword: str | None = None) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == func
        and (keyword is None or any(k.arg == keyword for k in node.keywords))
    ]


# --- the ingest half: rows are written ----------------------------------


def test_ingest_starts_the_sweep_that_fills_the_queue() -> None:
    """Without this task no notification is ever queued by anything."""
    main = _function(INGEST, "main")
    started = _calls(main, "notification_sweep_loop")
    assert started, "ingest main must start notification_sweep_loop"
    assert _calls(started[0], "build_obligation_notifier"), (
        "the sweep must be given the real notifier, over the process's engine"
    )


def test_the_sweep_is_started_as_a_task_rather_than_awaited() -> None:
    main = _function(INGEST, "main")
    assert [
        call
        for call in _calls(main, "create_task")
        if _calls(call, "notification_sweep_loop")
    ], "the sweep must run beside the other jobs, not block startup"


def test_an_operator_can_switch_the_queueing_off() -> None:
    source = INGEST.read_text()
    assert "settings.notifications_enabled" in source


def test_the_notifier_writes_through_the_postgres_queue() -> None:
    from chatmemory.composition import build_obligation_notifier

    notifier = build_obligation_notifier(
        Settings(**BASE),  # type: ignore[arg-type]
        create_engine_stub(),
    )
    assert isinstance(notifier, ObligationNotifier)
    assert isinstance(notifier._queue, PostgresNotificationQueue)  # noqa: SLF001


def create_engine_stub() -> object:
    """An engine handle that is never connected to.

    The builders take one and store it; nothing here executes a statement, so
    a real `create_async_engine` against an unreachable database is enough and
    is what the process would hand them.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine("postgresql+asyncpg://u:p@localhost/db")


# --- the bot half: rows are drained -------------------------------------


def test_bot_assemble_hands_build_bot_an_engine_for_notifications() -> None:
    assemble = _function(BOT, "assemble")
    passed = _calls(assemble, "build_bot", "notifications")
    assert passed, (
        "bot assemble must pass notifications= to build_bot, or the queue is "
        "filled by ingest and drained by nobody"
    )


def test_build_bot_builds_the_drain_and_attaches_the_switch() -> None:
    build_bot = _function(BOT, "build_bot")
    assert _calls(build_bot, "build_delivery"), "build_bot must build the drain"
    build_delivery = _function(BOT, "build_delivery")
    assert _calls(build_delivery, "attach_notifications"), (
        "/notifications must be attached to the client, or it is unavailable"
    )
    assert _calls(build_delivery, "build_notification_delivery")
    assert _calls(build_delivery, "DiscordNotificationSender"), (
        "the drain must be given something that can actually reach a person"
    )


def test_bot_main_runs_the_drain_beside_the_gateway() -> None:
    main = _function(BOT, "main")
    assert _calls(main, "notification_loop"), (
        "bot main must run notification_loop, or nothing ever drains the queue"
    )
    started = _calls(main, "notification_loop")[0]
    assert any(k.arg == "ready" for k in started.keywords), (
        "the drain must wait for the gateway: before it identifies, every "
        "member resolves to nothing readable"
    )


def test_the_drain_is_passed_to_the_runner_that_owns_the_process() -> None:
    main = _function(BOT, "main")
    run = _calls(main, "run_beside_scope")
    assert run, "bot main must still run the gateway beside the scope loop"
    assert any(
        isinstance(arg, ast.Name) and arg.id == "drains" for arg in run[0].args
    ), "the drain coroutine must be handed to run_beside_scope, or it never runs"


def test_the_command_is_registered_unconditionally() -> None:
    """The way out of an unsolicited message may not depend on configuration."""
    setup = _function(CLIENT, "setup_hook")
    assert _calls(setup, "_build_notifications_command"), (
        "/notifications must be registered on every deployment"
    )


def test_the_switch_and_the_drain_write_and_read_the_same_queue() -> None:
    """A preference stored somewhere the claim does not read turns nothing off."""
    from chatmemory.composition import (
        build_notification_delivery,
        build_notification_preferences,
    )

    engine = create_engine_stub()
    settings = Settings(**BASE)  # type: ignore[arg-type]
    preferences = build_notification_preferences(engine)  # type: ignore[arg-type]
    delivery = build_notification_delivery(
        settings,
        engine,  # type: ignore[arg-type]
        lambda: None,
        _NullSender(),
    )
    assert isinstance(preferences, NotificationPreferences)
    assert isinstance(delivery, NotificationDelivery)
    assert isinstance(preferences._queue, PostgresNotificationQueue)  # noqa: SLF001
    assert isinstance(delivery._queue, PostgresNotificationQueue)  # noqa: SLF001


class _NullSender:
    async def send(self, draft: object) -> object:  # pragma: no cover - never called
        raise AssertionError("the wiring test never delivers anything")


# --- the port contract --------------------------------------------------


@pytest.mark.parametrize("method", ["pending_for", "settle_unreadable"])
def test_every_method_that_decides_what_to_send_requires_a_viewer(
    method: str,
) -> None:
    """Sending without a send-time permission check must be unrepresentable."""
    import inspect

    signature = inspect.signature(getattr(NotificationQueue, method))
    assert "viewer" in signature.parameters, f"{method} must take a viewer"
    assert signature.parameters["viewer"].default is inspect.Parameter.empty, (
        f"{method}'s viewer must be required, not defaulted"
    )
