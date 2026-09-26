"""`PostgresPrivacyStore.inventory` against the real schema.

Every store in the design's table is seeded for one person and for somebody
else, and the inventory must hold the person's rows and nobody else's. Channel
content -- archived messages and their media -- is read only in the channels
passed as readable: a message in any other channel is neither counted nor
named. An unknown person reads as empty and is not created.

Run against a database at head (`alembic upgrade head`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.privacy_postgres import PostgresPrivacyStore
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.facts import FactKind
from chatmemory.ports.privacy import HeldFact, Inventory, MemoryCounts, NotificationSetting

pytestmark = pytest.mark.asyncio

PLATFORM = "discord"
NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
MONTH = date(2026, 9, 1)
ALICE = PersonRef(PLATFORM, 9101)
BOB = PersonRef(PLATFORM, 9102)
READABLE, HIDDEN = 910, 911
WALLET = "0x" + "ab" * 20


async def _exec(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


async def _person(engine: AsyncEngine, person: PersonRef, name: str) -> int:
    person_id = await _exec(
        engine, "INSERT INTO person (display_name) VALUES (:n) RETURNING id", n=name
    )
    await _exec(
        engine,
        "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
        "VALUES (:p, :u, :i)",
        p=PLATFORM,
        u=person.platform_user_id,
        i=person_id,
    )
    return int(str(person_id))


async def _message(engine: AsyncEngine, message_id: int, channel: int, author: int) -> None:
    await _exec(
        engine,
        "INSERT INTO channel (id, platform, name) VALUES (:c, :p, 'c') ON CONFLICT DO NOTHING",
        c=channel,
        p=PLATFORM,
    )
    await _exec(
        engine,
        "INSERT INTO message (id, channel_id, author_person_id, content, created_at) "
        "VALUES (:id, :c, :a, 'hello', :at)",
        id=message_id,
        c=channel,
        a=author,
        at=NOW,
    )


async def _media(engine: AsyncEngine, message_id: int, kind: str, transcript: str | None) -> None:
    await _exec(
        engine,
        "INSERT INTO message_media (message_id, attachment_id, kind, declared_type, byte_size, "
        "source_url, text, status) VALUES (:m, :a, :k, 'x/y', 10, 'https://cdn.example/x', :t, "
        "CASE WHEN CAST(:t AS text) IS NULL THEN 'pending' ELSE 'done' END)",
        m=message_id,
        a=message_id * 10,
        k=kind,
        t=transcript,
    )


async def _queue_notification(engine: AsyncEngine, person_id: int, message_id: int) -> None:
    """An ask in message `message_id` addressed to `person_id`, queued to tell them."""
    key = f"ask-{person_id}-{message_id}"
    await _exec(
        engine,
        "INSERT INTO ask (ask_key, source_message_id, channel_id, requester_person_id, "
        "addressee_kind, addressee_person_id, kind, text, confidence, asked_at) "
        "SELECT :k, id, channel_id, author_person_id, 'person', :p, 'request', 'review?', "
        "0.9, created_at FROM message WHERE id = :m",
        k=key,
        p=person_id,
        m=message_id,
    )
    await _exec(
        engine,
        "INSERT INTO notification (ask_key, person_id, channel_id, source_message_id) "
        "SELECT :k, :p, channel_id, id FROM message WHERE id = :m",
        k=key,
        p=person_id,
        m=message_id,
    )


async def _seed_personal(engine: AsyncEngine, person_id: int, platform_user_id: int) -> None:
    """One or more rows in every personal store for `person_id`."""
    statements = [
        "INSERT INTO person_fact (person_id, kind, value) VALUES (:i, 'email', 'a@example.com')",
        f"INSERT INTO person_fact (person_id, kind, value) VALUES (:i, 'eth_wallet', '{WALLET}')",
        "INSERT INTO conversation_turn (person_id, location_platform, location_id, "
        "location_direct, question, answer, channel_ids) "
        "VALUES (:i, 'discord', 1, TRUE, 'first question', 'a', '{}')",
        "INSERT INTO conversation_turn (person_id, location_platform, location_id, "
        "location_direct, question, answer, channel_ids) "
        "VALUES (:i, 'discord', 910, FALSE, 'second question', 'a', '{}')",
        "INSERT INTO conversation_summary (person_id, location_platform, location_id, "
        "location_direct, text, covered_channel_ids, through_turn_id, starts_at) "
        "VALUES (:i, 'discord', 1, TRUE, 'summary', '{}', 1, now())",
        "INSERT INTO scheduled_task (person_id, question, interval_hours, next_run_at) "
        "VALUES (:i, 'any news?', 12, now())",
        "INSERT INTO position_alert (person_id, kind, language, next_check_at, asset, "
        "direction, price_level) VALUES (:i, 'price', 'en', now(), 'BTC', 'above', 100000)",
        "INSERT INTO notification_preference (person_id, enabled) VALUES (:i, FALSE)",
        "INSERT INTO media_usage (person_id, month, purpose, seconds) "
        "VALUES (:i, DATE '2026-09-01', 'question', 75)",
        "INSERT INTO media_usage (person_id, month, purpose, seconds) "
        "VALUES (:i, DATE '2026-09-01', 'channel', 15)",
        "INSERT INTO media_usage (person_id, month, purpose, seconds) "
        "VALUES (:i, DATE '2026-08-01', 'question', 500)",
        "INSERT INTO feature_request (person_id, text, normalized_hash, source_kind, platform) "
        "VALUES (:i, 'a digest', decode(md5(:tag), 'hex'), 'command', 'discord')",
        "INSERT INTO mcp_token (token_hash, platform, platform_user_id, label) "
        "VALUES ('h' || :tag, 'discord', :u, 'laptop')",
        "INSERT INTO mcp_token (token_hash, platform, platform_user_id, label, revoked_at) "
        "VALUES ('r' || :tag, 'discord', :u, 'old', now())",
        "INSERT INTO trace_export (trace_id, created_at, asker_platform_user_id) "
        "VALUES ('t1-' || :tag, now(), :u)",
        "INSERT INTO trace_export (trace_id, created_at, asker_platform_user_id, deleted_at) "
        "VALUES ('t2-' || :tag, now(), :u, now())",
    ]
    for sql in statements:
        await _exec(engine, sql, i=person_id, u=platform_user_id, tag=str(platform_user_id))


async def test_the_inventory_holds_every_store_for_the_person_alone(clean: AsyncEngine) -> None:
    alice = await _person(clean, ALICE, "Alice")
    bob = await _person(clean, BOB, "Bob")
    await _seed_personal(clean, alice, ALICE.platform_user_id)
    await _seed_personal(clean, bob, BOB.platform_user_id)
    await _message(clean, 1, READABLE, alice)
    await _message(clean, 2, READABLE, alice)
    await _message(clean, 3, HIDDEN, alice)
    await _message(clean, 4, READABLE, bob)
    await _media(clean, 1, "image", "a cat")
    await _media(clean, 2, "voice", None)
    await _media(clean, 3, "voice", "hidden transcript")
    await _media(clean, 4, "image", None)
    await _queue_notification(clean, alice, 4)

    held = await PostgresPrivacyStore(clean).inventory(ALICE, [READABLE], MONTH)

    assert held.known and held.archiving
    assert held.platforms == ("discord",)
    assert held.facts == (
        HeldFact(FactKind.EMAIL, "a@example.com"),
        HeldFact(FactKind.ETH_WALLET, WALLET),
    )
    assert held.memory == MemoryCounts(
        direct_turns=1, direct_summaries=1, channel_turns=1, channel_summaries=0
    )
    assert set(held.recent_questions) == {"first question", "second question"}
    assert [(t.question, t.interval_hours, t.disabled) for t in held.tasks] == [
        ("any news?", 12, False)
    ]
    assert [(a.kind, a.asset, a.address) for a in held.alerts] == [("price", "BTC", None)]
    assert held.notifications == NotificationSetting(enabled=False, queued=1)
    assert held.voice_seconds_this_month == 90, "both purposes, this month only"
    assert [s.text for s in held.suggestions] == ["a digest"]
    assert [t.label for t in held.tokens] == ["laptop"], "a revoked token grants nothing"
    assert held.traces == 1, "a deleted trace is no longer held"
    assert [(a.channel, a.messages) for a in held.archived] == [
        (ChannelRef(PLATFORM, READABLE), 2)
    ]
    assert held.media.by_kind == (("image", 1), ("voice", 1))
    assert held.media.with_text == 1


async def test_a_channel_not_readable_is_neither_named_nor_counted(clean: AsyncEngine) -> None:
    alice = await _person(clean, ALICE, "Alice")
    await _message(clean, 3, HIDDEN, alice)
    await _media(clean, 3, "voice", "hidden transcript")

    held = await PostgresPrivacyStore(clean).inventory(ALICE, [READABLE], MONTH)
    none_readable = await PostgresPrivacyStore(clean).inventory(ALICE, [], MONTH)

    for inventory in (held, none_readable):
        assert inventory.archived == ()
        assert inventory.media.total == 0


async def test_an_opted_out_person_reads_as_not_archived(clean: AsyncEngine) -> None:
    alice = await _person(clean, ALICE, "Alice")
    await _exec(clean, "INSERT INTO person_opt_out (person_id) VALUES (:i)", i=alice)

    held = await PostgresPrivacyStore(clean).inventory(ALICE, [READABLE], MONTH)

    assert held.known and not held.archiving


async def test_an_unknown_person_is_empty_and_not_created(clean: AsyncEngine) -> None:
    held = await PostgresPrivacyStore(clean).inventory(ALICE, [READABLE], MONTH)

    assert held == Inventory()
    assert await _exec(clean, "SELECT count(*) FROM person") == 0
