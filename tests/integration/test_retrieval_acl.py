"""The invariants that only a real database can prove.

Chiefly: that permission filtering does not silently reduce how many results
a restricted viewer receives. That failure is invisible to unit tests -- the
code looks correct, every row returned is permitted, and the only symptom is
that people in fewer channels quietly get worse answers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import sql
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer

pytestmark = pytest.mark.asyncio

OPEN_CH, PRIVATE_CH = 100, 300
T0 = datetime(2026, 9, 13, tzinfo=UTC)
DIMS = 1536


def viewer(*channel_ids: int) -> Viewer:
    return Viewer(
        person=PersonRef("discord", 1),
        visible_channels=frozenset(ChannelRef("discord", c) for c in channel_ids),
    )


async def seed(engine: AsyncEngine, per_channel: int = 60) -> None:
    """Fill both channels with windows matching the same term.

    Enough rows that an approximate scan has to choose, which is the
    condition under which post-filtering under-returns.
    """
    async with engine.begin() as conn:
        for cid, name in ((OPEN_CH, "general"), (PRIVATE_CH, "leadership")):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT DO NOTHING"
                ),
                {"id": cid, "n": name},
            )
        for cid in (OPEN_CH, PRIVATE_CH):
            for i in range(per_channel):
                await conn.execute(
                    text(
                        "INSERT INTO conversation_window "
                        "(channel_id, text, starts_at, ends_at, search_tsv) "
                        "VALUES (:c, :t, :s, :s, to_tsvector('english', :t))"
                    ),
                    {
                        "c": cid,
                        "t": f"deployment rollout discussion number {i}",
                        "s": T0 + timedelta(minutes=i),
                    },
                )


async def test_restricted_viewer_sees_nothing_from_forbidden_channels(
    clean: AsyncEngine,
) -> None:
    await seed(clean)
    async with clean.connect() as conn:
        rows = await conn.execute(
            sql.LEXICAL_SEARCH,
            {
                "q": "deployment rollout",
                "channel_ids": [OPEN_CH],
                "since": None,
                "until": None,
                "limit": 100,
            },
        )
        channels = {r["channel_id"] for r in rows.mappings()}
    assert channels == {OPEN_CH}


async def test_restricted_viewer_still_receives_a_full_page(clean: AsyncEngine) -> None:
    """The under-return case, stated as a count.

    A membership-only assertion passes while the system under-returns, so
    this asserts the number of rows -- which is the thing that actually
    degrades for people in fewer channels.
    """
    await seed(clean, per_channel=60)
    wanted = 40
    async with clean.connect() as conn:
        rows = await conn.execute(
            sql.LEXICAL_SEARCH,
            {
                "q": "deployment rollout",
                "channel_ids": [OPEN_CH],
                "since": None,
                "until": None,
                "limit": wanted,
            },
        )
        returned = list(rows.mappings())
    assert len(returned) == wanted, (
        f"restricted viewer got {len(returned)} of {wanted} results; "
        "the filter is reducing the page rather than constraining the scan"
    )


async def test_vector_search_with_a_filter_returns_a_full_page(
    clean: AsyncEngine,
) -> None:
    """The same property for the approximate index, where it actually bites."""
    await seed(clean, per_channel=60)
    async with clean.begin() as conn:
        await conn.execute(
            text(
                "UPDATE conversation_window SET embedding = "
                "(SELECT array_agg(random())::vector FROM generate_series(1, :d))"
            ),
            {"d": DIMS},
        )

    wanted = 40
    async with clean.connect() as conn:
        for statement in sql.SESSION_SETUP:
            await conn.execute(text(statement))
        rows = await conn.execute(
            sql.VECTOR_SEARCH,
            {
                "embedding": sql.vector_literal([0.01] * DIMS),
                "channel_ids": [OPEN_CH],
                "since": None,
                "until": None,
                "limit": wanted,
            },
        )
        returned = list(rows.mappings())

    assert len(returned) == wanted
    assert {r["channel_id"] for r in returned} == {OPEN_CH}


async def test_empty_channel_set_returns_nothing(clean: AsyncEngine) -> None:
    """An unconstrained query here would return the whole corpus."""
    await seed(clean, per_channel=5)
    async with clean.connect() as conn:
        rows = await conn.execute(
            sql.LEXICAL_SEARCH,
            {
                "q": "deployment",
                "channel_ids": [],
                "since": None,
                "until": None,
                "limit": 10,
            },
        )
        assert list(rows.mappings()) == []


async def test_tombstoned_windows_are_never_returned(clean: AsyncEngine) -> None:
    await seed(clean, per_channel=5)
    async with clean.begin() as conn:
        await conn.execute(
            text("UPDATE conversation_window SET deleted_at = now() WHERE channel_id = :c"),
            {"c": OPEN_CH},
        )
    async with clean.connect() as conn:
        rows = await conn.execute(
            sql.LEXICAL_SEARCH,
            {
                "q": "deployment",
                "channel_ids": [OPEN_CH],
                "since": None,
                "until": None,
                "limit": 10,
            },
        )
        assert list(rows.mappings()) == []


async def test_time_bounds_are_applied(clean: AsyncEngine) -> None:
    await seed(clean, per_channel=30)
    cutoff = T0 + timedelta(minutes=20)
    async with clean.connect() as conn:
        rows = await conn.execute(
            sql.LEXICAL_SEARCH,
            {
                "q": "deployment",
                "channel_ids": [OPEN_CH],
                "since": cutoff,
                "until": None,
                "limit": 100,
            },
        )
        returned = list(rows.mappings())
    assert returned
    assert all(r["ends_at"] >= cutoff for r in returned)
