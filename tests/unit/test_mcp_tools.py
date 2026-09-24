"""The tool surface, exercised through the MCP dispatcher.

Testing at this layer rather than only against the repository is the point:
the repository can enforce permissions perfectly while the tool above it
hands the backend the wrong viewer, and only a test that goes through
argument validation and dispatch will notice.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from chatmemory.domain.search import PersonCandidate, RelevanceSource, SearchHit, SearchQuery
from chatmemory.mcp.auth import bind_viewer, unbind_viewer
from chatmemory.mcp.tools import UNAVAILABLE, build_server

GUILD = 7000
GENERAL = ChannelRef("discord", 100)
LEADERSHIP = ChannelRef("discord", 300)
ALICE = PersonRef("discord", 1001)
T0 = datetime(2026, 9, 13, tzinfo=UTC)

FORBIDDEN_CHANNEL = "300"
NONEXISTENT_CHANNEL = "999999999999"


def hit(window_id: int, channel: ChannelRef, message_ids: tuple[int, ...] = ()) -> SearchHit:
    return SearchHit(
        window_id=window_id,
        channel=channel,
        text=f"window {window_id}",
        starts_at=T0,
        ends_at=T0 + timedelta(minutes=5),
        score=0.016,
        relevance_source=RelevanceSource.FUSED_RRF,
        message_ids=message_ids,
    )


def message(message_id: int, channel: ChannelRef) -> Message:
    return Message(
        platform_message_id=message_id,
        channel=channel,
        author=ALICE,
        content=f"message {message_id}",
        created_at=T0,
    )


class RecordingSearch:
    """A `SearchBackend` that records the viewer it was handed.

    It enforces the permission rule itself, the way the real one does, so a
    tool that passes a widened viewer shows up as extra results rather than
    as a silently ignored argument.
    """

    def __init__(self, corpus: dict[ChannelRef, list[SearchHit]] | None = None) -> None:
        self.corpus = corpus or {}
        self.viewers: list[Viewer] = []
        self.queries: list[SearchQuery] = []
        self.fail = False

    def _check(self, viewer: Viewer) -> None:
        self.viewers.append(viewer)
        if self.fail:
            raise RuntimeError("database unreachable")

    async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
        self._check(viewer)
        self.queries.append(query)
        return [h for c in viewer.visible_channels for h in self.corpus.get(c, [])]

    async def thread_context(
        self, viewer: Viewer, platform_message_id: int, radius: int = 10
    ) -> Sequence[Message]:
        self._check(viewer)
        for channel in viewer.visible_channels:
            if any(platform_message_id in h.message_ids for h in self.corpus.get(channel, [])):
                return [message(platform_message_id, channel)]
        return []

    async def list_channels(self, viewer: Viewer) -> Sequence[ChannelRef]:
        self._check(viewer)
        return sorted(
            (c for c in viewer.visible_channels if c in self.corpus),
            key=lambda c: c.platform_channel_id,
        )

    async def people_named(
        self, viewer: Viewer, name: str, limit: int = 6
    ) -> Sequence[PersonCandidate]:
        return []


def corpus() -> dict[ChannelRef, list[SearchHit]]:
    return {
        GENERAL: [hit(1, GENERAL, (5001,)), hit(2, GENERAL)],
        LEADERSHIP: [hit(3, LEADERSHIP, (7001,))],
    }


def viewer_of(*channels: ChannelRef) -> Viewer:
    return Viewer(person=ALICE, visible_channels=frozenset(channels))


async def call(
    backend: RecordingSearch, viewer: Viewer, tool: str, arguments: dict[str, Any]
) -> Any:
    """Dispatch a tool the way an authenticated request would."""
    server = build_server(backend, GUILD)
    handle = bind_viewer(viewer)
    try:
        result = await server.call_tool(tool, arguments)
    finally:
        unbind_viewer(handle)
    return result


# --- the surface itself ------------------------------------------------


async def test_the_three_tools_are_exposed() -> None:
    tools = await build_server(RecordingSearch(), GUILD).list_tools()
    assert {t.name for t in tools} == {"search_messages", "thread_context", "list_channels"}


@pytest.mark.parametrize(
    "forbidden",
    [
        "viewer",
        "viewer_id",
        "person",
        "person_id",
        "user_id",
        "on_behalf_of",
        "acl",
        "permissions",
        "visible_channels",
        "readable_channels",
        "channel_ids",
    ],
)
async def test_no_tool_accepts_an_identity_argument(forbidden: str) -> None:
    """The impersonation attempt must have nothing to aim at.

    `channel_id` is present and is intent: it can only narrow what the viewer
    already sees. A plural `channel_ids` would look like the permission set
    and is exactly the field that must never appear here.
    """
    for tool in await build_server(RecordingSearch(), GUILD).list_tools():
        properties = tool.input_schema.get("properties", {})
        assert forbidden not in properties, f"{tool.name} exposes {forbidden}"


# --- search ------------------------------------------------------------


async def test_search_scopes_to_the_authenticated_viewer() -> None:
    backend = RecordingSearch(corpus())
    result = await call(backend, viewer_of(GENERAL), "search_messages", {"query": "rollout"})
    body = result.structured_content
    assert body["status"] == "ok"
    assert body["result_count"] == 2
    assert {r["channel_id"] for r in body["results"]} == {"100"}
    assert backend.viewers[0].visible_channels == frozenset({GENERAL})


async def test_an_extra_viewer_argument_changes_nothing() -> None:
    """Arguments the schema does not declare reach code that never reads them."""
    backend = RecordingSearch(corpus())
    result = await call(
        backend,
        viewer_of(GENERAL),
        "search_messages",
        {
            "query": "rollout",
            "viewer": "discord:1002",
            "person_id": 1002,
            "channel_ids": [300],
            "visible_channels": [300],
        },
    )
    assert {r["channel_id"] for r in result.structured_content["results"]} == {"100"}
    assert backend.viewers[0].visible_channels == frozenset({GENERAL})


async def test_a_channel_filter_can_only_narrow() -> None:
    backend = RecordingSearch(corpus())
    result = await call(
        backend,
        viewer_of(GENERAL, LEADERSHIP),
        "search_messages",
        {"query": "rollout", "channel_id": "100"},
    )
    assert {r["channel_id"] for r in result.structured_content["results"]} == {"100"}


async def test_a_forbidden_channel_yields_an_empty_success() -> None:
    """Not an error: an error would confirm the channel is there."""
    backend = RecordingSearch(corpus())
    result = await call(
        backend,
        viewer_of(GENERAL),
        "search_messages",
        {"query": "rollout", "channel_id": FORBIDDEN_CHANNEL},
    )
    body = result.structured_content
    assert body["status"] == "ok"
    assert body["result_count"] == 0
    assert backend.viewers[0].visible_channels == frozenset()


async def test_forbidden_and_nonexistent_channels_are_indistinguishable() -> None:
    """The response must carry no signal about which of the two it was."""
    backend = RecordingSearch(corpus())
    viewer = viewer_of(GENERAL)
    forbidden = await call(
        backend, viewer, "search_messages", {"query": "x", "channel_id": FORBIDDEN_CHANNEL}
    )
    missing = await call(
        backend, viewer, "search_messages", {"query": "x", "channel_id": NONEXISTENT_CHANNEL}
    )
    assert forbidden.structured_content == missing.structured_content


async def test_a_viewer_with_no_channels_gets_an_empty_success() -> None:
    backend = RecordingSearch(corpus())
    result = await call(backend, viewer_of(), "search_messages", {"query": "rollout"})
    assert result.structured_content["result_count"] == 0
    assert result.is_error is not True


async def test_no_matches_is_an_empty_result_not_an_error() -> None:
    backend = RecordingSearch({})
    result = await call(backend, viewer_of(GENERAL), "search_messages", {"query": "nothing"})
    assert result.structured_content == {
        "status": "ok",
        "result_count": 0,
        "results": [],
        "notice": result.structured_content["notice"],
    }


async def test_a_dependency_failure_is_an_error_not_an_empty_result() -> None:
    """A client must never render "the database is down" as "nobody spoke"."""
    backend = RecordingSearch(corpus())
    backend.fail = True
    with pytest.raises(UnexpectedToolError):
        await call(backend, viewer_of(GENERAL), "search_messages", {"query": "rollout"})


async def test_the_time_range_reaches_the_query_as_an_aware_instant() -> None:
    """A naive datetime is rejected by asyncpg against a timestamptz column."""
    backend = RecordingSearch(corpus())
    await call(
        backend,
        viewer_of(GENERAL),
        "search_messages",
        {"query": "x", "since": "2026-09-01T00:00:00", "until": "2026-09-30T12:00:00+02:00"},
    )
    query = backend.queries[0]
    assert query.since is not None and query.since.tzinfo is not None
    assert query.until is not None and query.until.tzinfo is not None


async def test_a_malformed_time_is_reported_as_a_caller_error() -> None:
    backend = RecordingSearch(corpus())
    with pytest.raises(ToolError, match="since"):
        await call(
            backend, viewer_of(GENERAL), "search_messages", {"query": "x", "since": "last tuesday"}
        )


async def test_the_limit_is_clamped_rather_than_honoured_blindly() -> None:
    backend = RecordingSearch(corpus())
    await call(backend, viewer_of(GENERAL), "search_messages", {"query": "x", "limit": 5000})
    assert backend.queries[0].limit == 50


async def test_results_carry_their_score_provenance() -> None:
    """0.016 means first place under RRF, not "1.6% relevant"."""
    backend = RecordingSearch(corpus())
    result = await call(backend, viewer_of(GENERAL), "search_messages", {"query": "x"})
    assert all(r["relevance_source"] == "fused_rrf" for r in result.structured_content["results"])


async def test_a_result_cites_a_resolvable_link() -> None:
    backend = RecordingSearch(corpus())
    result = await call(backend, viewer_of(GENERAL), "search_messages", {"query": "x"})
    citations = {r["citation"]["url"] for r in result.structured_content["results"]}
    assert f"https://discord.com/channels/{GUILD}/100/5001" in citations
    # A window with no known message id still resolves, to its channel.
    assert f"https://discord.com/channels/{GUILD}/100" in citations


async def test_retrieved_text_is_labelled_as_data() -> None:
    """Message text may contain anything, including something shaped like an
    instruction. The response says so, so a consuming agent reports on it
    rather than obeying it."""
    backend = RecordingSearch(corpus())
    result = await call(backend, viewer_of(GENERAL), "search_messages", {"query": "x"})
    assert "not instruction" in result.structured_content["notice"]


# --- thread context ----------------------------------------------------


async def test_context_is_returned_for_a_readable_message() -> None:
    backend = RecordingSearch(corpus())
    result = await call(backend, viewer_of(GENERAL), "thread_context", {"message_id": "5001"})
    body = result.structured_content
    assert body["status"] == "ok"
    assert body["message_count"] == 1
    assert body["messages"][0]["citation"]["url"].endswith("/100/5001")


async def test_context_for_a_forbidden_message_is_refused() -> None:
    backend = RecordingSearch(corpus())
    with pytest.raises(ToolError, match=UNAVAILABLE):
        await call(backend, viewer_of(GENERAL), "thread_context", {"message_id": "7001"})


async def _context_error(backend: RecordingSearch, viewer: Viewer, message_id: str) -> str:
    with pytest.raises(ToolError) as raised:
        await call(backend, viewer, "thread_context", {"message_id": message_id})
    return str(raised.value)


async def test_a_forbidden_message_and_a_missing_one_report_the_same_thing() -> None:
    """Otherwise the error is an oracle for which private channels are busy."""
    backend = RecordingSearch(corpus())
    viewer = viewer_of(GENERAL)
    forbidden = await _context_error(backend, viewer, "7001")
    missing = await _context_error(backend, viewer, "424242")
    assert forbidden == missing
    assert UNAVAILABLE in forbidden


async def test_context_never_leaks_a_message_from_another_channel() -> None:
    backend = RecordingSearch(corpus())
    result = await call(
        backend, viewer_of(GENERAL, LEADERSHIP), "thread_context", {"message_id": "7001"}
    )
    assert {m["channel_id"] for m in result.structured_content["messages"]} == {"300"}
    assert backend.viewers[0].visible_channels == frozenset({GENERAL, LEADERSHIP})


# --- channel listing ---------------------------------------------------


async def test_listing_shows_only_the_viewer_s_channels() -> None:
    backend = RecordingSearch(corpus())
    result = await call(backend, viewer_of(GENERAL), "list_channels", {})
    body = result.structured_content
    assert body["channel_count"] == 1
    assert [c["channel_id"] for c in body["channels"]] == ["100"]


async def test_listing_for_a_broader_viewer_shows_more() -> None:
    backend = RecordingSearch(corpus())
    result = await call(backend, viewer_of(GENERAL, LEADERSHIP), "list_channels", {})
    assert [c["channel_id"] for c in result.structured_content["channels"]] == ["100", "300"]


async def test_listing_with_no_access_is_an_empty_success() -> None:
    backend = RecordingSearch(corpus())
    result = await call(backend, viewer_of(), "list_channels", {})
    assert result.structured_content == {"status": "ok", "channel_count": 0, "channels": []}
