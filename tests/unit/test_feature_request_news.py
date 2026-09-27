"""The suggestion status sweep: one message per change, never an unsent claim kept."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from chatmemory.app.feature_request_news import StatusNewsRunner, status_message
from chatmemory.domain.identity import PersonRef
from chatmemory.entrypoints.bot import suggestion_news_loop
from chatmemory.health import HealthState
from chatmemory.ports.feature_requests import RequestStatus, StatusNews
from chatmemory.ports.notifications import DeliveryResult
from tests.unit.test_assembly_seam import BOT, _calls, _function

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
SAM = PersonRef("discord", 42)


def news(request_id: int = 12, language: str | None = "en", text: str = "dark mode") -> StatusNews:
    return StatusNews(
        request_id=request_id,
        person=SAM,
        status=RequestStatus.PLANNED,
        previous="new",
        language=language,
        text=text,
    )


@dataclass
class FakeStore:
    """Hands out each queued item once, as the claim does."""

    queued: list[StatusNews] = field(default_factory=list)
    released: list[StatusNews] = field(default_factory=list)
    undeliverable: list[PersonRef] = field(default_factory=list)

    async def claim_status_news(self, limit: int) -> Sequence[StatusNews]:
        claimed, self.queued = self.queued[:limit], self.queued[limit:]
        return claimed

    async def release(self, item: StatusNews) -> None:
        self.released.append(item)
        self.queued.append(item)

    async def record_undeliverable(self, person: PersonRef, now: datetime) -> None:
        self.undeliverable.append(person)


@dataclass
class FakeMessenger:
    result: DeliveryResult = DeliveryResult.SENT
    sent: list[tuple[PersonRef, int, str]] = field(default_factory=list)

    async def deliver(self, person: PersonRef, task_id: int, text: str) -> DeliveryResult:
        if self.result is DeliveryResult.SENT:
            self.sent.append((person, task_id, text))
        return self.result


def runner(store: FakeStore, messenger: FakeMessenger) -> StatusNewsRunner:
    return StatusNewsRunner(store, messenger, clock=lambda: NOW)


async def test_a_change_is_sent_once() -> None:
    store, messenger = FakeStore([news()]), FakeMessenger()

    assert await runner(store, messenger).run_due() == 1
    assert await runner(store, messenger).run_due() == 0

    assert [(p, i) for p, i, _ in messenger.sent] == [(SAM, 12)]
    assert store.released == []


async def test_a_closed_direct_message_is_put_back_and_recorded() -> None:
    store, messenger = FakeStore([news()]), FakeMessenger(DeliveryResult.CLOSED)

    assert await runner(store, messenger).run_due() == 0

    assert store.released == [news()]
    assert store.undeliverable == [SAM]


async def test_a_transient_failure_is_put_back_for_the_next_sweep() -> None:
    store, messenger = FakeStore([news()]), FakeMessenger(DeliveryResult.FAILED)

    await runner(store, messenger).run_due()

    assert store.released == [news()]
    assert store.undeliverable == []
    messenger.result = DeliveryResult.SENT
    assert await runner(store, messenger).run_due() == 1


@dataclass
class RaisingMessenger(FakeMessenger):
    """Raises on the first delivery, as a dropped socket or a timeout would."""

    raised: int = 0

    async def deliver(self, person: PersonRef, task_id: int, text: str) -> DeliveryResult:
        if self.raised == 0:
            self.raised += 1
            raise OSError("connection reset")
        return await super().deliver(person, task_id, text)


async def test_a_raised_delivery_is_put_back_and_the_batch_goes_on() -> None:
    # Regression: an exception from deliver() left the loop, and every claim
    # after it stayed advanced, so those messages were never sent.
    store, messenger = FakeStore([news(1), news(2)]), RaisingMessenger()

    assert await runner(store, messenger).run_due() == 1
    assert [i for _, i, _ in messenger.sent] == [2]
    assert store.released == [news(1)]

    assert await runner(store, messenger).run_due() == 1
    assert sorted(i for _, i, _ in messenger.sent) == [1, 2]


class FailingReleaseStore(FakeStore):
    async def release(self, item: StatusNews) -> None:
        self.released.append(item)
        raise OSError("database went away")


async def test_a_failed_put_back_does_not_abandon_the_rest() -> None:
    store = FailingReleaseStore([news(1), news(2)])

    assert await runner(store, FakeMessenger(DeliveryResult.FAILED)).run_due() == 0

    assert [n.request_id for n in store.released] == [1, 2]


def test_the_message_is_in_the_suggestions_language() -> None:
    assert status_message(news(language="pt")) == (
        "Sua sugestão **#12** agora está: **planejada**.\n> dark mode\n"
        "`/suggestions` mostra todas as suas."
    )
    assert status_message(news(language=None)).startswith(
        "Your suggestion **#12** is now **planned**."
    )


def test_a_long_suggestion_is_clipped_to_one_line() -> None:
    message = status_message(news(text="line one\nline two " + "x" * 300))

    quoted = message.splitlines()[1]
    assert quoted.startswith("> line one line two")
    assert quoted.endswith("…")
    assert len(quoted) == 2 + 200


# --- wiring: built beside /suggest, started beside the gateway ----------------------


def test_build_bot_builds_the_sweep_with_an_unheaded_messenger() -> None:
    [call] = _calls(_function(BOT, "build_bot"), "build_feature_request_news")
    messenger = call.args[1]
    assert ast.unparse(messenger) == "DiscordTaskMessenger(client.fetch_user, prefix='')"


def test_main_starts_the_sweep_only_when_it_was_built() -> None:
    main = _function(BOT, "main")
    [call] = _calls(main, "suggestion_news_loop")
    ready = next(k.value for k in call.keywords if k.arg == "ready")
    assert getattr(ready, "id", None) == "gateway_ready"
    [guard] = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "graph.suggestion_news is not None"
    ]
    guarded = [c for statement in guard.body for c in _calls(statement, "suggestion_news_loop")]
    assert guarded == [call]


async def test_the_sweep_waits_for_the_gateway() -> None:
    store, messenger = FakeStore([news()]), FakeMessenger()
    task = asyncio.ensure_future(
        suggestion_news_loop(runner(store, messenger), HealthState(), ready=asyncio.Event())
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert messenger.sent == []
