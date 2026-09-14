"""Reconciliation: repairing what the gateway never told us.

A deletion that happens while the process is down is not replayed on
reconnect -- the event is simply lost. Absence from a re-read of live history
is the only evidence it happened, which is what these tests exercise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from chatmemory.adapters.discord.source import Reconciler, reconcile_window
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

CH = ChannelRef("discord", 100)
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=24)
AUTHOR = PersonRef("discord", 7)


def msg(mid: int, content: str = "hi", edited: datetime | None = None) -> Message:
    """Message `mid`, posted `100 - mid` minutes ago.

    Higher id means newer, as snowflake ids do: the backfill cursor walks ids
    and would page the wrong way if the two orders disagreed.
    """
    return Message(
        platform_message_id=mid,
        channel=CH,
        author=AUTHOR,
        content=content,
        created_at=NOW - timedelta(minutes=100 - mid),
        edited_at=edited,
    )


class FakeHistory:
    """What the platform currently says the channel contains."""

    def __init__(self, messages: Sequence[Message]) -> None:
        self.messages = sorted(messages, key=lambda m: -m.platform_message_id)

    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]:
        candidates = [
            m
            for m in self.messages
            if before_message_id is None or m.platform_message_id < before_message_id
        ]
        return candidates[:limit]


class FakeLedger:
    def __init__(self, revisions: Mapping[int, datetime]) -> None:
        self.revisions = dict(revisions)
        self.asked_since: datetime | None = None

    async def stored_revisions(
        self, channel: ChannelRef, since: datetime
    ) -> Mapping[int, datetime]:
        self.asked_since = since
        return self.revisions


class FakeSink:
    def __init__(self) -> None:
        self.edited: list[Message] = []
        self.deleted: list[int] = []

    async def handle_edit(self, message: Message) -> None:
        self.edited.append(message)

    async def handle_delete(
        self, platform_message_id: int, at: datetime | None = None
    ) -> None:
        self.deleted.append(platform_message_id)


def build(
    live: Sequence[Message], stored: Mapping[int, datetime], page_size: int = 100
) -> tuple[Reconciler, FakeSink, FakeLedger]:
    sink, ledger = FakeSink(), FakeLedger(stored)
    reconciler = Reconciler(
        source=FakeHistory(live),
        ledger=ledger,
        sink=sink,
        page_size=page_size,
        now=lambda: NOW,
    )
    return reconciler, sink, ledger


async def test_message_deleted_while_offline_is_tombstoned() -> None:
    live = [msg(1), msg(3)]
    stored = {m: msg(m).revision for m in (1, 2, 3)}
    reconciler, sink, ledger = build(live, stored)

    report = await reconciler.reconcile(CH, SINCE)

    assert sink.deleted == [2]
    assert report.deleted == 1


async def test_message_edited_while_offline_is_reapplied() -> None:
    edited_at = NOW - timedelta(minutes=5)
    live = [msg(1, content="after", edited=edited_at)]
    reconciler, sink, ledger = build(live, {1: msg(1).revision})

    await reconciler.reconcile(CH, SINCE)

    assert [m.content for m in sink.edited] == ["after"]


async def test_message_posted_while_offline_is_captured() -> None:
    """The same comparison catches a message we never saw at all."""
    reconciler, sink, ledger = build([msg(1), msg(2)], {1: msg(1).revision})

    await reconciler.reconcile(CH, SINCE)

    assert [m.platform_message_id for m in sink.edited] == [2]
    assert sink.deleted == []


async def test_unchanged_history_is_left_alone() -> None:
    """The steady state must be free: reconciliation runs on every channel."""
    live = [msg(1), msg(2)]
    reconciler, sink, ledger = build(live, {m.platform_message_id: m.revision for m in live})

    report = await reconciler.reconcile(CH, SINCE)

    assert (sink.edited, sink.deleted) == ([], [])
    assert (report.updated, report.deleted) == (0, 0)


async def test_history_is_paged_backwards_until_the_lookback_boundary() -> None:
    live = [msg(i) for i in range(1, 8)]  # ids 1..7, oldest to newest
    reconciler, sink, ledger = build(live, {}, page_size=2)

    report = await reconciler.reconcile(CH, NOW - timedelta(minutes=96))

    # Only messages inside the lookback are examined; older ones are another
    # pass's problem, and re-reading a whole channel would not be affordable.
    assert report.scanned == 4
    assert sorted(m.platform_message_id for m in sink.edited) == [4, 5, 6, 7]


async def test_the_ledger_is_asked_for_the_same_window_that_was_scanned() -> None:
    """Absence *before* the lookback is not evidence of deletion.

    If the ledger returned messages older than the scanned history, every one
    of them would look deleted -- reconciliation would erase the archive it
    exists to protect.
    """
    reconciler, sink, ledger = build([msg(7)], {7: msg(7).revision})
    await reconciler.reconcile(CH, SINCE)
    assert ledger.asked_since == SINCE
    assert sink.deleted == []


def test_reconcile_window_looks_back_from_now() -> None:
    assert reconcile_window(timedelta(hours=6), NOW) == NOW - timedelta(hours=6)
