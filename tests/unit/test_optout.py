"""Per-person opt-out: what it covers, and the order it does it in.

Two of these are about sequencing rather than about outcomes, because the
failures they describe leave a corpus that looks purged and is not.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chatmemory.app.optout import OptOutService, PersonPurge
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message

pytestmark = pytest.mark.asyncio

ALICE = PersonRef("discord", 1)
BOB = PersonRef("discord", 2)
CHANNEL = ChannelRef("discord", 100)
T0 = datetime(2026, 9, 13, tzinfo=UTC)


def msg(mid: int, author: PersonRef) -> Message:
    return Message(
        platform_message_id=mid,
        channel=CHANNEL,
        author=author,
        content="hello",
        created_at=T0,
    )


class FakeRegistry:
    """Records the order operations arrived in, which is the thing under test."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.excluded: set[PersonRef] = set()
        self.purged: list[PersonRef] = []

    async def record_opt_out(self, person: PersonRef, reason: str = "") -> None:
        self.calls.append("record")
        self.excluded.add(person)

    async def clear_opt_out(self, person: PersonRef) -> None:
        self.calls.append("clear")
        self.excluded.discard(person)

    async def is_opted_out(self, person: PersonRef) -> bool:
        return person in self.excluded

    async def purge_person(self, person: PersonRef) -> PersonPurge:
        self.calls.append("purge")
        self.purged.append(person)
        return PersonPurge(windows=2, messages=7, asks=1, reactions=3, mentions=4)


class FakeDocuments:
    def __init__(self) -> None:
        self.purged: list[PersonRef] = []

    async def purge_person_documents(self, person: PersonRef) -> int:
        self.purged.append(person)
        return 5


async def test_the_exclusion_is_recorded_before_the_purge() -> None:
    """In the other order there is a window between the delete and the flag in
    which a backfill page re-imports exactly what was just removed -- and the
    opt-out still reports success."""
    registry = FakeRegistry()
    await OptOutService(registry, FakeDocuments()).opt_out(ALICE)

    assert registry.calls == ["record", "purge"]


async def test_it_covers_uploads_and_not_only_messages() -> None:
    """An opt-out that withdraws the message and leaves the attached PDF
    searchable has removed the index entry and kept the content."""
    documents = FakeDocuments()
    report = await OptOutService(FakeRegistry(), documents).opt_out(ALICE)

    assert documents.purged == [ALICE]
    assert report.documents == 5
    assert report.total == 22


async def test_without_a_document_store_the_report_shows_the_gap() -> None:
    report = await OptOutService(FakeRegistry()).opt_out(ALICE)

    assert report.documents == 0
    assert report.corpus.messages == 7


async def test_opting_back_in_clears_the_exclusion_and_restores_nothing() -> None:
    registry = FakeRegistry()
    service = OptOutService(registry, FakeDocuments())

    await service.opt_out(ALICE)
    await service.opt_in(ALICE)

    assert not await service.excludes(ALICE)
    # Purged is purged; only a later backfill of what the platform still holds
    # can bring anything back, and that is a new decision.
    assert registry.purged == [ALICE]
    assert registry.calls == ["record", "purge", "clear"]


async def test_an_opted_out_author_is_dropped_before_storage() -> None:
    registry = FakeRegistry()
    service = OptOutService(registry, FakeDocuments())
    await service.opt_out(ALICE)

    kept = await service.filter_messages([msg(1, ALICE), msg(2, BOB), msg(3, ALICE)])

    assert [m.platform_message_id for m in kept] == [2]


async def test_filtering_nobody_returns_the_page_unchanged() -> None:
    service = OptOutService(FakeRegistry(), FakeDocuments())
    page = [msg(1, ALICE), msg(2, BOB)]

    assert await service.filter_messages(page) == page
    assert await service.filter_messages([]) == []
