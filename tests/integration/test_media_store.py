"""Pending media rows against a real database.

What is asserted here is what Postgres does with them: that the capture
transaction writes a row only for a message it actually stored, that a
re-read refreshes the URL and never the status, and that every path which
removes a message -- deletion, retention, opt-out, a channel purge -- removes
or withdraws what hangs off it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.source import Reconciler
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.media import MediaKind, MediaRef
from chatmemory.domain.messages import Message

pytestmark = pytest.mark.asyncio

CHANNEL = ChannelRef("discord", 710)
ALICE = PersonRef("discord", 7101)
BOB = PersonRef("discord", 7102)

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
ENABLED = NOW - timedelta(days=1)


def note(attachment_id: int, url: str = "https://cdn.discordapp.com/a/1/v.ogg?ex=1") -> MediaRef:
    return MediaRef(
        attachment_id=attachment_id,
        kind=MediaKind.VOICE,
        content_type="audio/ogg",
        byte_size=48_000,
        url=url,
        filename="voice-message.ogg",
        duration_seconds=6.5,
    )


def msg(mid: int, *media: MediaRef, at: datetime = NOW, author: PersonRef = ALICE) -> Message:
    return Message(mid, CHANNEL, author, "", at, media=tuple(media))


def store(engine: AsyncEngine) -> PostgresStore:
    return PostgresStore(engine, media_since=ENABLED)


async def rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT message_id, attachment_id, kind, status, attempts, source_url, text "
                "FROM message_media ORDER BY message_id, attachment_id"
            )
        )
        return [dict(r) for r in result.mappings()]


async def set_row(engine: AsyncEngine, assignments: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"UPDATE message_media SET {assignments}"))


# --- capture -----------------------------------------------------------


async def test_a_captured_voice_note_is_one_pending_row(clean: AsyncEngine) -> None:
    await store(clean).upsert_messages([msg(1, note(10))])

    assert await rows(clean) == [
        {
            "message_id": 1,
            "attachment_id": 10,
            "kind": "voice",
            "status": "pending",
            "attempts": 0,
            "source_url": note(10).url,
            "text": None,
        }
    ]


async def test_nothing_is_recorded_before_media_is_enabled(clean: AsyncEngine) -> None:
    await PostgresStore(clean).upsert_messages([msg(1, note(10))])
    await store(clean).upsert_messages([msg(2, note(20), at=ENABLED - timedelta(seconds=1))])

    assert await rows(clean) == []


async def test_a_message_created_exactly_at_the_moment_is_recorded(
    clean: AsyncEngine,
) -> None:
    """"At or after": the cutoff itself is inside the window."""
    await store(clean).upsert_messages([msg(1, note(10), at=ENABLED)])

    assert [r["message_id"] for r in await rows(clean)] == [1]


class LiveHistory:
    """What Discord says the channel holds now, newest first."""

    def __init__(self, *messages: Message) -> None:
        self._messages = sorted(messages, key=lambda m: -m.platform_message_id)

    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]:
        older = [
            m
            for m in self._messages
            if before_message_id is None or m.platform_message_id < before_message_id
        ]
        return older[:limit]


class StoreSink:
    """Reconciliation's corrections, written straight to the store."""

    def __init__(self, corpus: PostgresStore) -> None:
        self._corpus = corpus

    async def handle_edit(self, message: Message) -> None:
        await self._corpus.upsert_messages([message])

    async def handle_delete(self, platform_message_id: int, at: datetime | None = None) -> None:
        await self._corpus.tombstone_message(platform_message_id, at or NOW)


async def test_a_backfill_window_does_not_reach_history_already_imported(
    clean: AsyncEngine,
) -> None:
    """MEDIA_BACKFILL_DAYS applies to what ingest writes from now on, not to
    what it already holds: an unchanged message is never written again.

    Pins what docs/operations.md promises. Should a re-read of imported history
    for media ever be added, this is the test that changes.
    """
    in_window = ENABLED - timedelta(days=2)
    imported = msg(1, note(10), at=in_window)
    await PostgresStore(clean).upsert_messages([imported])  # before media was on

    missed = msg(2, note(20), at=in_window + timedelta(hours=1))  # posted while down
    widened = PostgresStore(clean, media_since=ENABLED - timedelta(days=7))
    reconciler = Reconciler(LiveHistory(imported, missed), widened, StoreSink(widened))
    await reconciler.reconcile(CHANNEL, ENABLED - timedelta(days=7))

    assert [r["message_id"] for r in await rows(clean)] == [2]


async def test_a_re_read_refreshes_the_url_and_never_the_status(clean: AsyncEngine) -> None:
    corpus = store(clean)
    await corpus.upsert_messages([msg(1, note(10)), msg(2, note(20))])
    await set_row(clean, "attempts = 2 WHERE message_id = 1")
    await set_row(clean, "status = 'failed', skip_reason = 'fetch_failed' WHERE message_id = 2")

    fresh = "https://cdn.discordapp.com/a/1/v.ogg?ex=2"
    await corpus.upsert_messages([msg(1, note(10, fresh)), msg(2, note(20, fresh))])

    first, second = await rows(clean)
    assert (first["status"], first["attempts"], first["source_url"]) == ("pending", 2, fresh)
    # Finished with: a new URL is no reason to try it again.
    assert (second["status"], second["source_url"]) == ("failed", note(20).url)


async def test_an_edit_that_drops_an_attachment_drops_its_row(clean: AsyncEngine) -> None:
    corpus = store(clean)
    both = msg(1, note(10), note(11))
    await corpus.upsert_messages([both])

    edited = replace(both, edited_at=NOW + timedelta(minutes=1), media=(note(11),))
    await corpus.upsert_messages([edited])

    assert [r["attachment_id"] for r in await rows(clean)] == [11]


async def test_a_message_deleted_before_it_landed_gets_no_row(clean: AsyncEngine) -> None:
    corpus = store(clean)
    await corpus.tombstone_message(1, NOW)

    await corpus.upsert_messages([msg(1, note(10))])

    assert await rows(clean) == []


async def test_an_opted_out_authors_voice_note_gets_no_row(clean: AsyncEngine) -> None:
    """The 0008 trigger refuses the message, and the row has nothing to hang off."""
    corpus = store(clean)
    await corpus.upsert_messages([msg(1, author=ALICE)])
    await OptOutService(PostgresRetentionStore(clean)).opt_out(ALICE)

    await corpus.upsert_messages([msg(2, note(20), author=ALICE)])

    assert await rows(clean) == []


# --- removal -----------------------------------------------------------


async def test_deleting_a_message_withdraws_its_media_in_the_same_step(
    clean: AsyncEngine,
) -> None:
    corpus = store(clean)
    await corpus.upsert_messages([msg(1, note(10)), msg(2, note(20))])
    await set_row(clean, "status = 'done', text = 'o deploy é sexta' WHERE message_id = 1")

    await corpus.tombstone_message(1, NOW)

    withdrawn, kept = await rows(clean)
    assert (withdrawn["status"], withdrawn["text"], withdrawn["source_url"]) == (
        "withdrawn",
        None,
        "",
    )
    assert kept["status"] == "pending"
    # And a re-read of the deleted message brings nothing back.
    await corpus.upsert_messages([msg(1, note(10))])
    assert (await rows(clean))[0]["status"] == "withdrawn"


async def test_retention_takes_the_rows_with_the_messages(clean: AsyncEngine) -> None:
    corpus = PostgresStore(clean, media_since=NOW - timedelta(days=90))
    await corpus.upsert_messages([msg(1, note(10), at=NOW - timedelta(days=40)), msg(2, note(20))])

    await PostgresRetentionStore(clean).purge_corpus_before(NOW - timedelta(days=30))

    assert [r["message_id"] for r in await rows(clean)] == [2]


async def test_opting_out_takes_the_rows_with_the_messages(clean: AsyncEngine) -> None:
    await store(clean).upsert_messages(
        [msg(1, note(10), author=ALICE), msg(2, note(20), author=BOB)]
    )

    await OptOutService(PostgresRetentionStore(clean)).opt_out(ALICE)

    assert [r["message_id"] for r in await rows(clean)] == [2]


async def test_a_channel_leaving_scope_takes_the_rows_with_it(clean: AsyncEngine) -> None:
    corpus = store(clean)
    await corpus.upsert_messages([msg(1, note(10))])

    await corpus.purge_channel(CHANNEL)

    assert await rows(clean) == []
