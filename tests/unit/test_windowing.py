"""Windowing: the choice that most determines whether retrieval works."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from chatmemory.app.windowing import WindowBuilder, approximate_tokens
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

CHANNEL = ChannelRef("discord", 1)
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def msg(
    mid: int,
    minutes: float = 0,
    content: str = "hello",
    thread: int | None = None,
    deleted: bool = False,
) -> Message:
    return Message(
        platform_message_id=mid,
        channel=CHANNEL,
        author=PersonRef("discord", 10),
        content=content,
        created_at=T0 + timedelta(minutes=minutes),
        thread_id=thread,
        deleted_at=T0 if deleted else None,
    )


def test_consecutive_messages_form_one_window() -> None:
    windows = WindowBuilder().build(CHANNEL, [msg(1), msg(2, 1), msg(3, 2)])
    assert len(windows) == 1
    assert windows[0].message_ids == (1, 2, 3)


def test_long_silence_splits_a_window() -> None:
    """A conversation resuming hours later is not the same conversation."""
    builder = WindowBuilder(gap=timedelta(minutes=15))
    windows = builder.build(CHANNEL, [msg(1), msg(2, 1), msg(3, 120)])
    assert [w.message_ids for w in windows] == [(1, 2), (3,)]


def test_message_count_caps_a_window() -> None:
    builder = WindowBuilder(max_messages=3)
    windows = builder.build(CHANNEL, [msg(i, i * 0.1) for i in range(1, 8)])
    assert [len(w.message_ids) for w in windows] == [3, 3, 1]


def test_token_budget_caps_a_window() -> None:
    """Sizing is in tokens, so one long message can close a window early."""
    builder = WindowBuilder(max_messages=100, max_tokens=40)
    windows = builder.build(
        CHANNEL, [msg(1, 0, "x" * 90), msg(2, 1, "y" * 90), msg(3, 2, "z" * 90)]
    )
    assert len(windows) > 1


def test_threads_are_grouped_separately() -> None:
    """A thread is already a conversation; interleaving it loses that."""
    windows = WindowBuilder().build(
        CHANNEL,
        [msg(1), msg(2, 1, thread=500), msg(3, 2), msg(4, 3, thread=500)],
    )
    by_thread = {w.thread_id: w.message_ids for w in windows}
    assert by_thread[None] == (1, 3)
    assert by_thread[500] == (2, 4)


def test_deleted_messages_never_enter_a_window() -> None:
    windows = WindowBuilder().build(CHANNEL, [msg(1), msg(2, 1, deleted=True), msg(3, 2)])
    assert windows[0].message_ids == (1, 3)
    assert "hello" in windows[0].text


def test_window_carries_its_time_span() -> None:
    windows = WindowBuilder().build(CHANNEL, [msg(1), msg(2, 5)])
    assert windows[0].starts_at == T0
    assert windows[0].ends_at == T0 + timedelta(minutes=5)


def test_empty_input_yields_no_windows() -> None:
    assert WindowBuilder().build(CHANNEL, []) == []


def test_all_deleted_yields_no_windows() -> None:
    assert WindowBuilder().build(CHANNEL, [msg(1, deleted=True)]) == []


def test_windows_are_ordered_oldest_first() -> None:
    windows = WindowBuilder(max_messages=1).build(CHANNEL, [msg(1), msg(2, 30), msg(3, 60)])
    assert [w.starts_at for w in windows] == sorted(w.starts_at for w in windows)


def test_token_approximation_is_conservative() -> None:
    """Over-counting splits early; under-counting truncates at the model."""
    assert approximate_tokens("hello world") >= 1
    assert approximate_tokens("") == 1
