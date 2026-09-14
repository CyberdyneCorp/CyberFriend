"""Ask invariants that only a real database can prove.

Chiefly the permission one. An ask is a summary of a conversation, so an ask
that escapes its channel leaks more than the quote it came from would -- and a
count that includes an unreadable ask discloses the conversation just as surely
as returning it. Both are asserted here against real SQL rather than against a
fake that was written by the same hand as the code.

Also here: the state transitions, which are SQL rather than Python in the path
that actually runs, and the tombstone rule, which spans two tables.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.asks_postgres import PostgresAskStore
from chatmemory.app.asks.model import Ask as AskRecord
from chatmemory.app.asks.model import (
    AskKind,
    AskStatus,
    Correction,
    CorrectionOutcome,
    CorrectionResolution,
    ObligationRequest,
    ask_key,
    to_group,
    to_person,
)
from chatmemory.app.asks.state import ACKNOWLEDGING_REACTIONS
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
OPEN_CH = ChannelRef(PLATFORM, 100)
PRIVATE_CH = ChannelRef(PLATFORM, 300)

ALICE = PersonRef(PLATFORM, 1)
BOB = PersonRef(PLATFORM, 2)
CARA = PersonRef(PLATFORM, 3)

T0 = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
LATER = T0 + timedelta(minutes=5)

THREAD = 77


@pytest.fixture(autouse=True)
async def _requires_ask_schema(clean: AsyncEngine) -> None:
    async with clean.connect() as conn:
        present = await conn.execute(text("SELECT to_regclass('public.ask')"))
        if present.scalar() is None:
            pytest.skip("ask tables are missing; run `alembic upgrade head`")


def viewer(person: PersonRef, *channels: ChannelRef) -> Viewer:
    return Viewer(person=person, visible_channels=frozenset(channels))


async def seed_corpus(engine: AsyncEngine) -> None:
    """Two channels, three people, and one message per channel to hang asks on."""
    async with engine.begin() as conn:
        for channel, name in ((OPEN_CH, "general"), (PRIVATE_CH, "leadership")):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT DO NOTHING"
                ),
                {"id": channel.platform_channel_id, "n": name},
            )
        for person, name in ((ALICE, "alice"), (BOB, "bob"), (CARA, "cara")):
            created = await conn.execute(
                text("INSERT INTO person (display_name) VALUES (:n) RETURNING id"),
                {"n": name},
            )
            await conn.execute(
                text(
                    "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                    "VALUES (:p, :u, :i)"
                ),
                {"p": PLATFORM, "u": person.platform_user_id, "i": created.scalar_one()},
            )


async def add_message(
    engine: AsyncEngine,
    message_id: int,
    author: PersonRef,
    channel: ChannelRef,
    content: str = "can you review the migration?",
    at: datetime | None = None,
    thread_id: int | None = None,
    reply_to_id: int | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO message (id, channel_id, author_person_id, content, "
                "created_at, thread_id, reply_to_id) "
                "SELECT :id, :c, p.person_id, :content, :at, "
                "CAST(:thread AS bigint), CAST(:reply AS bigint) "
                "FROM person_platform_id p "
                "WHERE p.platform = :platform AND p.platform_user_id = :author"
            ),
            {
                "id": message_id,
                "c": channel.platform_channel_id,
                "content": content,
                "at": at or T0,
                "thread": thread_id,
                "reply": reply_to_id,
                "platform": PLATFORM,
                "author": author.platform_user_id,
            },
        )


def an_ask(
    source_message_id: int,
    channel: ChannelRef = OPEN_CH,
    requester: PersonRef = ALICE,
    addressee: PersonRef | None = BOB,
    kind: AskKind = AskKind.REQUEST,
    confidence: float = 0.9,
    asked_at: datetime | None = None,
    thread_id: int | None = None,
) -> AskRecord:
    target = to_person(addressee) if addressee is not None else to_group("the team")
    return AskRecord(
        key=ask_key(source_message_id, kind, target),
        source_message_id=source_message_id,
        channel=channel,
        requester=requester,
        addressee=target,
        kind=kind,
        text="review the migration",
        confidence=confidence,
        asked_at=asked_at or T0,
        thread_id=thread_id,
    )


async def test_asks_in_unreadable_channels_are_not_returned_or_counted(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    await add_message(clean, 20, ALICE, PRIVATE_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10)])
    await store.record_asks(20, [an_ask(20, channel=PRIVATE_CH)])

    restricted = viewer(BOB, OPEN_CH)
    found = await store.obligations(restricted, ObligationRequest())
    assert [r.ask.source_message_id for r in found] == [10]
    assert await store.count_outstanding(restricted, ObligationRequest()) == 1

    everything = viewer(BOB, OPEN_CH, PRIVATE_CH)
    assert await store.count_outstanding(everything, ObligationRequest()) == 2


async def test_a_viewer_with_no_channels_reads_nothing(clean: AsyncEngine) -> None:
    """An unconstrained query here would return every obligation in the server."""
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10)])

    assert await store.obligations(viewer(BOB), ObligationRequest()) == []
    assert await store.count_outstanding(viewer(BOB), ObligationRequest()) == 0


async def test_only_the_viewers_own_asks_are_returned(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    await add_message(clean, 11, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10, addressee=BOB)])
    await store.record_asks(11, [an_ask(11, addressee=CARA)])

    found = await store.obligations(viewer(BOB, OPEN_CH), ObligationRequest())
    assert [r.ask.source_message_id for r in found] == [10]


async def test_a_commitment_is_returned_to_the_person_who_made_it(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, BOB, OPEN_CH, content="i'll push the fix tonight")
    store = PostgresAskStore(clean)
    await store.record_asks(
        10, [an_ask(10, requester=BOB, addressee=BOB, kind=AskKind.COMMITMENT)]
    )

    found = await store.obligations(viewer(BOB, OPEN_CH), ObligationRequest())
    assert [r.ask.kind for r in found] == [AskKind.COMMITMENT]

    excluded = await store.obligations(
        viewer(BOB, OPEN_CH),
        ObligationRequest(
            kinds=frozenset({AskKind.REQUEST, AskKind.QUESTION}), include_commitments=False
        ),
    )
    assert excluded == []


async def test_a_group_ask_is_nobodys_personal_obligation(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10, addressee=None)])

    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 0


async def test_a_deleted_source_message_stops_the_ask_being_reported(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10)])
    reader = viewer(BOB, OPEN_CH)
    assert await store.count_outstanding(reader, ObligationRequest()) == 1

    async with clean.begin() as conn:
        await conn.execute(
            text("UPDATE message SET deleted_at = now() WHERE id = 10")
        )

    assert await store.obligations(reader, ObligationRequest()) == []
    assert await store.count_outstanding(reader, ObligationRequest()) == 0


async def test_recording_the_same_asks_twice_does_not_duplicate(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10)])
    await store.record_asks(10, [an_ask(10)])

    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 1


async def test_an_ask_no_longer_extracted_is_withdrawn(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10)])
    await store.record_asks(10, [])

    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 0


async def test_low_confidence_asks_are_filtered_in_the_query(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10, confidence=0.2)])

    reader = viewer(BOB, OPEN_CH)
    assert await store.count_outstanding(reader, ObligationRequest(min_confidence=0.6)) == 0
    assert await store.count_outstanding(reader, ObligationRequest(min_confidence=0.0)) == 1


async def test_the_period_is_applied(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH, at=T0 - timedelta(days=30))
    await add_message(clean, 11, ALICE, OPEN_CH, at=T0)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10, asked_at=T0 - timedelta(days=30))])
    await store.record_asks(11, [an_ask(11, asked_at=T0)])

    found = await store.obligations(
        viewer(BOB, OPEN_CH), ObligationRequest(since=T0 - timedelta(days=1))
    )
    assert [r.ask.source_message_id for r in found] == [11]


# --- state -------------------------------------------------------------


async def test_a_reply_in_thread_by_the_addressee_closes_the_ask(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH, thread_id=THREAD)
    await add_message(clean, 11, BOB, OPEN_CH, content="done", at=LATER, thread_id=THREAD)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10, thread_id=THREAD)])

    refreshed = await store.refresh_state(LATER, timedelta(days=21), ACKNOWLEDGING_REACTIONS)

    assert refreshed.answered_by_reply == 1
    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 0


async def test_unrelated_chatter_does_not_close_the_ask(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH, thread_id=THREAD)
    await add_message(clean, 11, BOB, OPEN_CH, content="lunch?", at=LATER)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10, thread_id=THREAD)])

    refreshed = await store.refresh_state(LATER, timedelta(days=21), ACKNOWLEDGING_REACTIONS)

    assert refreshed.answered_by_reply == 0
    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 1


async def test_an_acknowledging_reaction_closes_the_ask(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10)])
    await store.record_reaction(10, BOB, "✅", LATER)

    refreshed = await store.refresh_state(LATER, timedelta(days=21), ACKNOWLEDGING_REACTIONS)

    assert refreshed.answered_by_reaction == 1
    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 0


async def test_a_reaction_from_somebody_else_does_not_close_the_ask(
    clean: AsyncEngine,
) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10)])
    await store.record_reaction(10, CARA, "✅", LATER)

    refreshed = await store.refresh_state(LATER, timedelta(days=21), ACKNOWLEDGING_REACTIONS)
    assert refreshed.answered_by_reaction == 0


async def test_ageing_marks_stale_and_the_ask_is_still_reported(
    clean: AsyncEngine,
) -> None:
    """Silently closing it would hide exactly what the person asked about."""
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10)])

    refreshed = await store.refresh_state(
        T0 + timedelta(days=22), timedelta(days=21), ACKNOWLEDGING_REACTIONS
    )

    assert refreshed.marked_stale == 1
    found = await store.obligations(viewer(BOB, OPEN_CH), ObligationRequest())
    assert [r.ask.status for r in found] == [AskStatus.STALE]


async def test_re_extraction_does_not_reopen_an_answered_ask(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH, thread_id=THREAD)
    await add_message(clean, 11, BOB, OPEN_CH, content="done", at=LATER, thread_id=THREAD)
    store = PostgresAskStore(clean)
    await store.record_asks(10, [an_ask(10, thread_id=THREAD)])
    await store.refresh_state(LATER, timedelta(days=21), ACKNOWLEDGING_REACTIONS)

    await store.record_asks(10, [an_ask(10, thread_id=THREAD)])

    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 0


# --- corrections -------------------------------------------------------


async def test_the_addressee_can_correct_their_own_ask(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    item = an_ask(10)
    await store.record_asks(10, [item])

    outcome = await store.apply_correction(
        viewer(BOB, OPEN_CH),
        Correction(item.key, BOB, CorrectionResolution.NOT_APPLICABLE),
    )

    assert outcome is CorrectionOutcome.APPLIED
    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 0


async def test_somebody_else_cannot_correct_it(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    item = an_ask(10)
    await store.record_asks(10, [item])

    outcome = await store.apply_correction(
        viewer(CARA, OPEN_CH), Correction(item.key, CARA, CorrectionResolution.DONE)
    )

    assert outcome is CorrectionOutcome.NOT_ADDRESSEE
    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 1


async def test_a_correction_survives_re_extraction(clean: AsyncEngine) -> None:
    """The dismissed ask must not reappear when its window is reprocessed."""
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    item = an_ask(10)
    await store.record_asks(10, [item])
    await store.apply_correction(
        viewer(BOB, OPEN_CH), Correction(item.key, BOB, CorrectionResolution.DONE)
    )

    await store.record_asks(10, [an_ask(10)])

    assert await store.count_outstanding(viewer(BOB, OPEN_CH), ObligationRequest()) == 0
    async with clean.connect() as conn:
        surviving = await conn.execute(text("SELECT count(*) FROM ask_correction"))
        assert surviving.scalar_one() == 1


async def test_a_corrected_ask_is_not_pruned_away(clean: AsyncEngine) -> None:
    """Pruning it would let the next pass recreate it as a fresh obligation."""
    await seed_corpus(clean)
    await add_message(clean, 10, ALICE, OPEN_CH)
    store = PostgresAskStore(clean)
    item = an_ask(10)
    await store.record_asks(10, [item])
    await store.apply_correction(
        viewer(BOB, OPEN_CH), Correction(item.key, BOB, CorrectionResolution.DONE)
    )

    await store.record_asks(10, [])

    async with clean.connect() as conn:
        rows = await conn.execute(
            text("SELECT count(*) FROM ask WHERE ask_key = :k"), {"k": item.key}
        )
        assert rows.scalar_one() == 1


async def test_correcting_an_unreadable_ask_reports_unknown(clean: AsyncEngine) -> None:
    await seed_corpus(clean)
    await add_message(clean, 20, ALICE, PRIVATE_CH)
    store = PostgresAskStore(clean)
    item = an_ask(20, channel=PRIVATE_CH)
    await store.record_asks(20, [item])

    outcome = await store.apply_correction(
        viewer(BOB, OPEN_CH), Correction(item.key, BOB, CorrectionResolution.DONE)
    )
    assert outcome is CorrectionOutcome.UNKNOWN_ASK
