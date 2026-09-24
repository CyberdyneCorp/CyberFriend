"""Author-scoped retrieval and name resolution over a real database.

`SearchQuery.authors` was once silently ignored by the Postgres backend: a
caller asking for one person's words got everybody's. What is asserted here
is what the store does with it -- that another channel, another author, a
tombstone and a message outside the span each stay out, and that a name
only resolves to people who speak where the viewer can read. Each of those is
a predicate in `AUTHOR_SEARCH` or `PEOPLE_VISIBLE`, not a filter afterwards.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.postgres import HybridSearch, PostgresStore
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message, Window
from chatmemory.domain.search import SearchQuery

pytestmark = pytest.mark.asyncio

GENERAL, LEADERSHIP = 100, 300
DIMS = 1536
T0 = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

LEO = PersonRef("discord", 910_001)
BEA = PersonRef("discord", 910_002)
LIA = PersonRef("discord", 910_003)
JOAO_SILVA = PersonRef("discord", 910_004)
JOAO_PEREIRA = PersonRef("discord", 910_005)
JOAO_COSTA = PersonRef("discord", 910_006)


def ch(cid: int) -> ChannelRef:
    return ChannelRef("discord", cid)


def viewer(*channel_ids: int) -> Viewer:
    return Viewer(PersonRef("discord", 1), frozenset(ch(c) for c in channel_ids))


class FakeEmbeddings:
    dimensions = DIMS

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls += 1
        return [self._vector(t) for t in texts]

    def _vector(self, value: str) -> list[float]:
        digest = hashlib.sha256(value.encode()).digest()
        raw = (digest * (DIMS // len(digest) + 1))[:DIMS]
        return [b / 255.0 + 0.01 for b in raw]


_ids = iter(range(5_000_000, 6_000_000))


async def window(
    store: PostgresStore,
    cid: int,
    lines: Sequence[tuple[PersonRef, str, str, datetime]],
) -> list[int]:
    """One conversation window of (author, display, body, at) lines."""
    channel = ch(cid)
    messages = [
        Message(next(_ids), channel, who, body, at, author_display=display)
        for who, display, body, at in lines
    ]
    await store.upsert_messages(messages)
    rendered = "\n".join(f"{m.author_display}: {m.content}" for m in messages)
    await store.replace_windows(
        channel,
        [
            Window(
                channel,
                tuple(m.platform_message_id for m in messages),
                rendered,
                messages[0].created_at,
                messages[-1].created_at,
            )
        ],
    )
    return [m.platform_message_id for m in messages]


async def seed(engine: AsyncEngine) -> dict[str, int]:
    store = PostgresStore(engine)
    ids: dict[str, int] = {}
    ids["leo_general"], ids["bea_general"] = await window(
        store,
        GENERAL,
        [
            (LEO, "Leo", "the deploy goes out on thursday", T0),
            (BEA, "Bea", "the deploy needs a rollback plan first", T0 + timedelta(minutes=1)),
        ],
    )
    [ids["leo_private"]] = await window(
        store, LEADERSHIP, [(LEO, "Leo", "the deploy delays the reorg", T0)]
    )
    [ids["leo_old"]] = await window(
        store, GENERAL, [(LEO, "Leo", "the deploy was planned", T0 - timedelta(days=20))]
    )
    [ids["leo_deleted"]] = await window(
        store, GENERAL, [(LEO, "Leo", "the deploy password is hunter2", T0 + timedelta(hours=1))]
    )
    await store.tombstone_message(ids["leo_deleted"], T0 + timedelta(hours=2))
    [ids["lia_private"]] = await window(
        store, LEADERSHIP, [(LIA, "Lia", "layoffs are planned", T0)]
    )
    for person, display in (
        (JOAO_SILVA, "João Silva"),
        (JOAO_PEREIRA, "Joao Pereira"),
    ):
        await window(store, GENERAL, [(person, display, "hello", T0)])
    await window(store, LEADERSHIP, [(JOAO_COSTA, "João Costa", "private hello", T0)])
    embeddings = FakeEmbeddings()
    for pending in await store.windows_missing_embeddings(100):
        [vector] = await embeddings.embed([pending.text])
        assert pending.window_id is not None
        await store.store_embedding(pending.window_id, vector)
    return ids


def search(engine: AsyncEngine) -> HybridSearch:
    return HybridSearch(engine, FakeEmbeddings())


async def test_only_the_authors_lines_from_readable_channels_come_back(
    clean: AsyncEngine,
) -> None:
    ids = await seed(clean)
    hits = await search(clean).search(
        viewer(GENERAL), SearchQuery(text="deploy", authors=frozenset({LEO}))
    )

    cited = {mid for hit in hits for mid in hit.message_ids}
    assert ids["leo_general"] in cited and ids["leo_old"] in cited
    assert ids["bea_general"] not in cited, "another author's line in the same window"
    assert ids["leo_private"] not in cited, "a channel the viewer cannot read"
    assert ids["leo_deleted"] not in cited, "a tombstoned message"
    text = "\n".join(hit.text for hit in hits)
    assert "rollback" not in text and "reorg" not in text and "hunter2" not in text
    assert all(hit.author_display == "Leo" for hit in hits)
    assert all(hit.channel == ch(GENERAL) for hit in hits)


async def test_the_span_is_half_open_and_applied_in_the_statement(clean: AsyncEngine) -> None:
    ids = await seed(clean)
    backend = search(clean)
    inside = await backend.search(
        viewer(GENERAL, LEADERSHIP),
        SearchQuery(
            text="",
            authors=frozenset({LEO}),
            since=T0 - timedelta(days=1),
            until=T0 + timedelta(minutes=1),
        ),
    )
    cited = {mid for hit in inside for mid in hit.message_ids}
    assert cited == {ids["leo_general"], ids["leo_private"]}

    at_the_end = await backend.search(
        viewer(GENERAL),
        SearchQuery(text="", authors=frozenset({LEO}), since=T0 - timedelta(days=1), until=T0),
    )
    assert at_the_end == [], "a message at `until` is outside a half-open span"


async def test_no_topic_costs_no_embedding(clean: AsyncEngine) -> None:
    await seed(clean)
    embeddings = FakeEmbeddings()
    hits = await HybridSearch(clean, embeddings).search(
        viewer(GENERAL), SearchQuery(text="  ", authors=frozenset({LEO}))
    )
    assert hits and embeddings.calls == 0


async def test_an_empty_author_set_finds_nothing(clean: AsyncEngine) -> None:
    await seed(clean)
    hits = await search(clean).search(
        viewer(GENERAL), SearchQuery(text="deploy", authors=frozenset())
    )
    assert hits == []


async def test_a_viewer_with_no_channels_finds_nothing(clean: AsyncEngine) -> None:
    await seed(clean)
    query = SearchQuery(text="deploy", authors=frozenset({LEO}))
    hits = await search(clean).search(viewer(), query)
    assert hits == []


async def test_a_name_resolves_only_among_people_the_viewer_sees_speak(
    clean: AsyncEngine,
) -> None:
    await seed(clean)
    backend = search(clean)

    found = await backend.people_named(viewer(GENERAL), "joao")
    assert {c.ref for c in found} == {JOAO_SILVA, JOAO_PEREIRA}, (
        "João Costa speaks only in #leadership"
    )
    wider = await backend.people_named(viewer(GENERAL, LEADERSHIP), "João")
    assert {c.ref for c in wider} == {JOAO_SILVA, JOAO_PEREIRA, JOAO_COSTA}

    assert await backend.people_named(viewer(GENERAL), "Lia") == []
    [lia] = await backend.people_named(viewer(LEADERSHIP), "lia")
    assert lia.ref == LIA and lia.display == "Lia"


async def test_a_full_name_beats_a_first_name(clean: AsyncEngine) -> None:
    await seed(clean)
    [silva] = await search(clean).people_named(viewer(GENERAL), "Joao Silva")
    assert silva.ref == JOAO_SILVA


async def test_a_person_whose_only_visible_message_was_deleted_is_no_candidate(
    clean: AsyncEngine,
) -> None:
    store = PostgresStore(clean)
    ghost = PersonRef("discord", 910_099)
    [mid] = await window(store, GENERAL, [(ghost, "Gustavo", "bye", T0)])
    await store.tombstone_message(mid, T0 + timedelta(minutes=1))
    assert await search(clean).people_named(viewer(GENERAL), "Gustavo") == []
