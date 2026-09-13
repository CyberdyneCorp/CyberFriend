"""Citations have to land on the message, not near it.

A citation exists so a reader can check the claim in one click. A link to the
channel asks them to go and find it themselves among everything else said
that week, which is the same as not citing at all -- and `message_id = 0` is
worse, because it looks like a resolved reference and is not one.

The defect these cover: the search backend left `SearchHit.message_ids` empty
and the statement that resolves them, `sql.WINDOW_MESSAGE_IDS`, was executed
nowhere. Every citation the live path produced pointed at a channel.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store import sql
from chatmemory.adapters.store.postgres import HybridSearch
from chatmemory.app.reasoning.retrieval import CorpusRetrieval, discord_urls
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery

GUILD = 7
GENERAL = 100
ASKER = PersonRef("discord", 1)
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def ch(channel_id: int) -> ChannelRef:
    return ChannelRef("discord", channel_id)


def hit(window_id: int, *message_ids: int) -> SearchHit:
    return SearchHit(
        window_id=window_id,
        channel=ch(GENERAL),
        text="we rolled the deploy back at noon",
        starts_at=NOW,
        ends_at=NOW,
        score=0.016,
        relevance_source=RelevanceSource.FUSED_RRF,
        message_ids=tuple(message_ids),
    )


class StubSearch:
    """A backend that returns exactly the hits it was handed."""

    def __init__(self, *hits: SearchHit) -> None:
        self._hits = hits

    async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
        return self._hits

    async def thread_context(
        self, viewer: Viewer, platform_message_id: int, radius: int = 10
    ) -> Sequence[object]:
        return []

    async def list_channels(self, viewer: Viewer) -> Sequence[ChannelRef]:
        return []


async def retrieve(*hits: SearchHit) -> tuple[Any, ...]:
    retrieval = CorpusRetrieval(cast(Any, StubSearch(*hits)), discord_urls(GUILD))
    result = await retrieval.retrieve(
        Viewer(ASKER, frozenset({ch(GENERAL)})), SearchQuery(text="the deploy")
    )
    return result.items


# --- what a hydrated hit turns into -------------------------------------


async def test_a_citation_links_to_the_message_it_rests_on() -> None:
    (evidence,) = await retrieve(hit(1, 5551, 5552, 5553))

    assert evidence.url == f"https://discord.com/channels/{GUILD}/{GENERAL}/5551"
    assert evidence.citation().message_id == 5551


async def test_the_link_lands_on_the_window_rather_than_in_the_middle_of_it() -> None:
    """The opening message, so the reader arrives where the thread starts."""
    (evidence,) = await retrieve(hit(1, 900, 901, 902))
    assert evidence.citation().message_id == 900


async def test_a_window_with_no_resolvable_message_still_links_somewhere_real() -> None:
    """A channel link is a worse destination than the message, and a far
    better one than `/0`, which resolves to nothing at all."""
    (evidence,) = await retrieve(hit(1))

    assert evidence.url == f"https://discord.com/channels/{GUILD}/{GENERAL}"
    assert not evidence.url.endswith("/0")


# --- the backend that has to resolve them -------------------------------


class FakeResult:
    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> Sequence[Mapping[str, object]]:
        return self._rows


class FakeConnection:
    """Enough of an `AsyncConnection` to drive `HybridSearch.search`.

    It dispatches on the statement rather than returning a constant, so a
    test can assert that the membership query actually ran -- the defect was
    that `WINDOW_MESSAGE_IDS` was defined and never executed.
    """

    def __init__(self, lexical: Sequence[int], membership: Sequence[tuple[int, int]]) -> None:
        self._lexical = lexical
        self._membership = membership
        self.statements: list[str] = []

    async def execute(
        self, statement: object, params: Mapping[str, object] | None = None
    ) -> FakeResult:
        rendered = str(statement)
        self.statements.append(rendered)
        if "ts_rank_cd" in rendered:
            return FakeResult([self._window_row(w) for w in self._lexical])
        if "conversation_window_message" in rendered:
            return FakeResult(
                [{"window_id": w, "message_id": m} for w, m in self._membership]
            )
        return FakeResult([])

    def _window_row(self, window_id: int) -> Mapping[str, object]:
        return {
            "id": window_id,
            "channel_id": GENERAL,
            "text": "we rolled the deploy back at noon",
            "starts_at": NOW,
            "ends_at": NOW,
            "score": 0.9,
        }

    async def __aenter__(self) -> FakeConnection:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def connect(self) -> FakeConnection:
        return self._connection


class FakeEmbeddings:
    @property
    def dimensions(self) -> int:
        return 3

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0, 0.0, 0.0] for _ in texts]


def search_backend(
    lexical: Sequence[int], membership: Sequence[tuple[int, int]]
) -> tuple[HybridSearch, FakeConnection]:
    connection = FakeConnection(lexical, membership)
    backend = HybridSearch(
        cast(AsyncEngine, cast(Any, FakeEngine(connection))), cast(Any, FakeEmbeddings())
    )
    return backend, connection


async def test_the_backend_resolves_the_messages_behind_every_hit() -> None:
    backend, connection = search_backend(
        lexical=[1, 2], membership=[(1, 11), (1, 12), (2, 21)]
    )

    hits = await backend.search(
        Viewer(ASKER, frozenset({ch(GENERAL)})), SearchQuery(text="the deploy")
    )

    by_window = {h.window_id: h.message_ids for h in hits}
    assert by_window == {1: (11, 12), 2: (21,)}
    assert any("conversation_window_message" in s for s in connection.statements)


async def test_message_order_within_a_window_is_preserved() -> None:
    """The statement orders by position; the grouping must not undo that."""
    backend, _ = search_backend(lexical=[1], membership=[(1, 300), (1, 100), (1, 200)])

    (only,) = await backend.search(
        Viewer(ASKER, frozenset({ch(GENERAL)})), SearchQuery(text="the deploy")
    )
    assert only.message_ids == (300, 100, 200)


async def test_a_window_whose_membership_is_missing_is_still_returned() -> None:
    """A hit with no resolvable messages is worth less, not nothing."""
    backend, _ = search_backend(lexical=[1], membership=[])

    (only,) = await backend.search(
        Viewer(ASKER, frozenset({ch(GENERAL)})), SearchQuery(text="the deploy")
    )
    assert only.window_id == 1
    assert only.message_ids == ()


async def test_no_membership_query_is_issued_when_nothing_was_found() -> None:
    backend, connection = search_backend(lexical=[], membership=[])

    assert not await backend.search(
        Viewer(ASKER, frozenset({ch(GENERAL)})), SearchQuery(text="the deploy")
    )
    assert not any("conversation_window_message" in s for s in connection.statements)


def test_the_membership_statement_is_reachable_from_the_search_path() -> None:
    """Structural: the defect was a statement nobody executed.

    Stated at the source rather than only through the fake above, because a
    refactor that drops the call would otherwise only fail a test whose own
    fake could be adjusted to agree with it.
    """
    source = inspect.getsource(HybridSearch)
    assert "WINDOW_MESSAGE_IDS" in source
    assert "_with_message_ids" in source


def test_the_membership_statement_orders_by_position() -> None:
    """Grouping reconstructs each window's sequence from row order alone."""
    statement = str(sql.WINDOW_MESSAGE_IDS)
    assert "ORDER BY window_id, position" in statement
