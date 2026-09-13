"""Window rebuild: the three defects the audit reproduced.

All three came from one trigger -- "this message belongs to no live window" --
which can only ever fire once per message. Each test below reproduces the
original failure, so a regression shows up as the exact symptom rather than as
an abstract assertion about the trigger.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.ingest import IngestService
from chatmemory.app.windowing import WindowBuilder
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

pytestmark = pytest.mark.asyncio

CH_ID = 4242
CH = ChannelRef("discord", CH_ID)
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
LOOKBACK = timedelta(minutes=30)


class NoSource:
    async def backfill(self, channel: object, before: object, limit: int) -> list[Message]:
        return []

    def stream(self) -> object:  # pragma: no cover - unused
        raise NotImplementedError


def msg(mid: int, minutes: float = 0, content: str = "hello") -> Message:
    return Message(
        platform_message_id=mid,
        channel=CH,
        author=PersonRef("discord", 7),
        content=content,
        created_at=T0 + timedelta(minutes=minutes),
    )


async def setup(engine: AsyncEngine) -> IngestService:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO channel (id, platform, name, is_indexed) "
                "VALUES (:c,'discord','general',TRUE) ON CONFLICT DO NOTHING"
            ),
            {"c": CH_ID},
        )
    store = PostgresStore(engine)
    return IngestService(NoSource(), store, WindowBuilder(), frozenset({CH_ID}))  # type: ignore[arg-type]


async def live_windows(engine: AsyncEngine) -> list[tuple[int, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, text FROM conversation_window "
                "WHERE channel_id = :c AND deleted_at IS NULL ORDER BY starts_at"
            ),
            {"c": CH_ID},
        )
        return [(int(r[0]), str(r[1])) for r in rows]


async def rebuild(service: IngestService, since: datetime = T0) -> int:
    return await service.rewindow(CH, since - LOOKBACK)


async def test_consecutive_messages_merge_into_one_window(clean: AsyncEngine) -> None:
    """The defect: each tick produced a single-message window.

    Five messages a minute apart, well inside the gap and the count cap,
    became five windows -- which defeats the entire reason retrieval embeds
    windows rather than individual messages.
    """
    service = await setup(clean)
    for i in range(1, 6):
        await service.capture(msg(i, minutes=i, content=f"part {i} of one conversation"))
        await rebuild(service)  # tick the loop between each, as production does

    windows = await live_windows(clean)
    assert len(windows) == 1, f"expected one merged window, got {len(windows)}"
    for i in range(1, 6):
        assert f"part {i}" in windows[0][1]


async def test_edited_message_re_forms_its_window(clean: AsyncEngine) -> None:
    """The defect: an edited message was already windowed, so it never returned
    to the rebuild list and its window kept the pre-edit text forever."""
    service = await setup(clean)
    await service.capture(msg(1, content="the prod db password is hunter2"))
    await rebuild(service)
    assert "hunter2" in (await live_windows(clean))[0][1]

    edited = Message(
        platform_message_id=1,
        channel=CH,
        author=PersonRef("discord", 7),
        content="oops nevermind",
        created_at=T0,
        edited_at=T0 + timedelta(minutes=1),
    )
    await service.handle_edit(edited)
    await rebuild(service)

    windows = await live_windows(clean)
    assert windows, "the window should be re-formed, not removed"
    assert "hunter2" not in windows[0][1]
    assert "oops nevermind" in windows[0][1]


async def test_delete_during_rebuild_cannot_publish_retracted_text(
    clean: AsyncEngine,
) -> None:
    """The defect: a delete landing before the window existed tombstoned
    nothing, and the rebuild then inserted a live window carrying the text."""
    service = await setup(clean)
    await service.capture(msg(1, content="keep me"))
    await service.capture(msg(2, minutes=1, content="the prod db password is hunter2"))

    # Deleted before any window exists -- the race the audit reproduced.
    await service.handle_delete(2, channel=CH, created_at=T0)
    await rebuild(service)

    windows = await live_windows(clean)
    assert all("hunter2" not in text for _, text in windows)
    assert any("keep me" in text for _, text in windows), (
        "the surviving neighbour must remain retrievable"
    )


async def test_deleting_one_message_keeps_its_neighbours(clean: AsyncEngine) -> None:
    service = await setup(clean)
    await service.capture(msg(1, content="first"))
    await service.capture(msg(2, minutes=1, content="secret"))
    await service.capture(msg(3, minutes=2, content="third"))
    await rebuild(service)

    await service.handle_delete(2, channel=CH, created_at=T0 + timedelta(minutes=1))
    await rebuild(service)

    windows = await live_windows(clean)
    blob = " ".join(t for _, t in windows)
    assert "secret" not in blob
    assert "first" in blob and "third" in blob


async def test_rebuild_converges(clean: AsyncEngine) -> None:
    """A second pass must be a no-op, or the loop re-embeds forever."""
    service = await setup(clean)
    for i in range(1, 4):
        await service.capture(msg(i, minutes=i))
    await rebuild(service)
    before = await live_windows(clean)
    await rebuild(service)
    after = await live_windows(clean)
    assert [t for _, t in before] == [t for _, t in after]
