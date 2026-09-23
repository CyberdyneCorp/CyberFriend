"""Position alerts are built, reached and started -- and only when switched on.

The feature ships dark: with the defaults nothing is built and nothing starts,
so production behaves exactly as before. With the switch and an Infura key the
sweep is built over the process's transport and started beside the scheduled
sweep, on the edges' clock, after the gateway identifies.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from chatmemory.adapters.discord.bot import SCHEDULED_PREFIX, DiscordTaskMessenger
from chatmemory.app.alerts import AlertRunner
from chatmemory.composition import build_alert_runner
from chatmemory.config import Settings
from chatmemory.domain.identity import PersonRef
from chatmemory.entrypoints.bot import alert_loop
from chatmemory.health import HealthState
from chatmemory.ports.notifications import DeliveryResult
from tests.unit.test_assembly_seam import _calls, _function

ROOT = Path(__file__).resolve().parents[2]
BOT = ROOT / "src" / "chatmemory" / "entrypoints" / "bot.py"
COMPOSE = (ROOT / "docker-compose.yml").read_text()

BASE = {
    "discord_token": "x",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "llm_api_key": "k",
}


def settings(**overrides: object) -> Settings:
    return Settings.model_validate({**BASE, **overrides})


class _Messenger:
    async def deliver(self, person: PersonRef, task_id: int, text: str) -> DeliveryResult:
        return DeliveryResult.SENT


# --- off by default ---------------------------------------------------------------


def test_the_defaults_are_off_and_every_five_minutes() -> None:
    defaults = settings()
    assert defaults.alerts_enabled is False
    assert defaults.alert_sweep_seconds == 300


def test_nothing_is_built_unless_switched_on() -> None:
    assert build_alert_runner(settings(infura_key="k"), object(), _Messenger()) is None  # type: ignore[arg-type]


def test_nothing_is_built_without_an_infura_key() -> None:
    assert build_alert_runner(settings(alerts_enabled=True), object(), _Messenger()) is None  # type: ignore[arg-type]


def test_switched_on_with_a_key_the_sweep_is_built() -> None:
    runner = build_alert_runner(
        settings(alerts_enabled=True, infura_key="k"), object(), _Messenger()  # type: ignore[arg-type]
    )
    assert isinstance(runner, AlertRunner)


def test_a_sweep_under_a_minute_is_refused_at_boot() -> None:
    with pytest.raises(ValidationError):
        settings(alert_sweep_seconds=30)


def test_both_settings_reach_the_bot_with_their_defaults() -> None:
    """Declared, or the platform refuses them; defaulted, so an unset one is
    the documented default rather than an empty string."""
    bot = re.search(r"^  bot:$(.*?)(?=^  \w+:$|\Z)", COMPOSE, re.M | re.S)
    assert bot is not None
    assert "ALERTS_ENABLED=${ALERTS_ENABLED:-false}" in bot.group(1)
    assert "ALERT_SWEEP_SECONDS=${ALERT_SWEEP_SECONDS:-300}" in bot.group(1)


# --- reached ------------------------------------------------------------------------


def test_build_bot_builds_the_sweep_over_the_transport_it_is_handed() -> None:
    [call] = _calls(_function(BOT, "build_bot"), "build_alert_runner")
    transport = next(k.value for k in call.keywords if k.arg == "transport")
    assert getattr(transport, "id", None) == "alert_transport"


def test_assemble_hands_build_bot_the_edges_transport() -> None:
    [call] = _calls(_function(BOT, "assemble"), "build_bot")
    transport = next(k.value for k in call.keywords if k.arg == "alert_transport")
    assert getattr(transport, "attr", None) == "http_transport"


def test_main_starts_the_sweep_only_when_it_was_built() -> None:
    main = _function(BOT, "main")
    [call] = _calls(main, "alert_loop")
    interval = next(k.value for k in call.keywords if k.arg == "interval")
    ready = next(k.value for k in call.keywords if k.arg == "ready")
    assert getattr(interval, "attr", None) == "alert_sweep_seconds"
    assert getattr(ready, "id", None) == "gateway_ready"
    [guard] = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "graph.alerts is not None"
    ]
    guarded = [c for statement in guard.body for c in _calls(statement, "alert_loop")]
    assert guarded == [call], "alert_loop is started only under the guard"


async def test_the_sweep_waits_for_the_gateway() -> None:
    class Runner:
        calls = 0

        async def run_due(self, now: Any) -> int:
            Runner.calls += 1
            return 0

    task = asyncio.ensure_future(
        alert_loop(Runner(), HealthState(), ready=asyncio.Event())  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert Runner.calls == 0


# --- the messenger ------------------------------------------------------------------


class _Recipient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str, **_: object) -> None:
        self.sent.append(content)


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [(SCHEDULED_PREFIX, f"{SCHEDULED_PREFIX}\nhello"), ("", "hello")],
    ids=["scheduled", "alert"],
)
async def test_the_messenger_heads_a_message_only_when_given_a_heading(
    prefix: str, expected: str
) -> None:
    recipient = _Recipient()

    async def user(_: int) -> _Recipient:
        return recipient

    messenger = DiscordTaskMessenger(user, prefix=prefix)

    assert await messenger.deliver(PersonRef("discord", 1), 1, "hello") is DeliveryResult.SENT
    assert recipient.sent == [expected]
