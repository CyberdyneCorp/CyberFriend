"""The MCP surface end to end: real HTTP, real MCP client, real database.

Everything below the tool boundary is genuine -- the statements that carry
the permission predicate, the token table, the fused ranking. Only the guild
(permissions come from Discord, which a test cannot hold) and the embedding
endpoint (which costs money) are stood in for.

The test that matters most is `test_alice_cannot_retrieve_as_bob_by_any_means`:
if the endpoint is reachable from the internet, that property is the one
keeping a private channel private.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
import pytest_asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette

from chatmemory.adapters.store.postgres import HybridSearch
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.mcp.auth import (
    Authenticator,
    PostgresTokenStore,
    ensure_token_schema,
)
from chatmemory.mcp.server import build_app
from chatmemory.mcp.tools import UNAVAILABLE

pytestmark = pytest.mark.asyncio

GUILD = 7000
GENERAL, LEADERSHIP = 100, 300
ALICE = PersonRef("discord", 1001)
BOB = PersonRef("discord", 1002)
T0 = datetime(2026, 9, 13, tzinfo=UTC)
DIMS = 1536

# Distinct vocabularies, so a leak is unambiguous rather than a ranking
# coincidence.
OPEN_TERM = "deployment"
PRIVATE_TERM = "compensation"

# The seeded author's Discord account. Deliberately far from any person row
# id, so a tool that reports the row id instead is caught.
AUTHOR_DISCORD_ID = 880_001

GENERAL_MESSAGE_ID = 500_001
LEADERSHIP_MESSAGE_ID = 700_001
MISSING_MESSAGE_ID = 424_242

BASE_URL = "http://127.0.0.1"


class FakeEmbeddings:
    """Deterministic vectors: the vector leg must run, not be accurate."""

    @property
    def dimensions(self) -> int:
        return DIMS

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.001] * DIMS for _ in texts]


class FakeAcl:
    """Stands in for the guild cache.

    Alice reads the open channel; Bob reads both. The resolver is the only
    thing that decides this -- no token and no request field contributes.
    """

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        readable = {
            ALICE: frozenset({ChannelRef("discord", GENERAL)}),
            BOB: frozenset({ChannelRef("discord", GENERAL), ChannelRef("discord", LEADERSHIP)}),
        }
        return Viewer(person=person, visible_channels=readable.get(person, frozenset()))


async def seed(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        author = (
            await conn.execute(
                text("INSERT INTO person (display_name) VALUES ('seed') RETURNING id")
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO person_platform_id (platform, platform_user_id, person_id) "
                "VALUES ('discord', :u, :p)"
            ),
            {"u": AUTHOR_DISCORD_ID, "p": author},
        )

        for channel_id, name in ((GENERAL, "general"), (LEADERSHIP, "leadership")):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE)"
                ),
                {"id": channel_id, "n": name},
            )

        for channel_id, message_id, term in (
            (GENERAL, GENERAL_MESSAGE_ID, OPEN_TERM),
            (LEADERSHIP, LEADERSHIP_MESSAGE_ID, PRIVATE_TERM),
        ):
            await conn.execute(
                text(
                    "INSERT INTO message "
                    "(id, channel_id, author_person_id, content, created_at, search_tsv) "
                    "VALUES (:id, :c, :a, :t, :ts, to_tsvector('english', :t))"
                ),
                {
                    "id": message_id,
                    "c": channel_id,
                    "a": author,
                    "t": f"{term} conversation anchor",
                    "ts": T0,
                },
            )
            for i in range(5):
                await conn.execute(
                    text(
                        "INSERT INTO conversation_window "
                        "(channel_id, text, starts_at, ends_at, search_tsv) "
                        "VALUES (:c, :t, :s, :e, to_tsvector('english', :t))"
                    ),
                    {
                        "c": channel_id,
                        "t": f"{term} thread number {i}",
                        "s": T0 + timedelta(minutes=i),
                        "e": T0 + timedelta(minutes=i + 1),
                    },
                )


@pytest_asyncio.fixture
async def corpus(clean: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    await ensure_token_schema(clean)
    async with clean.begin() as conn:
        await conn.execute(text("DELETE FROM mcp_token"))
    await seed(clean)
    yield clean


@pytest_asyncio.fixture
async def tokens(corpus: AsyncEngine) -> PostgresTokenStore:
    return PostgresTokenStore(corpus)


AppFactory = Callable[[], Starlette]


@pytest_asyncio.fixture
async def app(corpus: AsyncEngine, tokens: PostgresTokenStore) -> AppFactory:
    """Builds a fresh application per connection.

    A factory rather than one instance because the streamable-HTTP session
    manager may be run exactly once; the state that matters to these tests
    lives in Postgres, which every instance shares.
    """

    def build() -> Starlette:
        return build_app(
            search=HybridSearch(corpus, FakeEmbeddings()),
            authenticator=Authenticator(tokens=tokens, acl=FakeAcl()),
            guild_id=GUILD,
        )

    return build


@asynccontextmanager
async def http_for(app: AppFactory, token: str | None, **headers: str) -> AsyncIterator[Any]:
    """An HTTP client against a running application.

    The lifespan is entered here rather than in a fixture because it owns a
    task group: leaving it in pytest's teardown task rather than the task
    that entered it is something anyio refuses outright.
    """
    sent = dict(headers)
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    application = app()
    async with application.router.lifespan_context(application), httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=application), base_url=BASE_URL, headers=sent
    ) as http:
        yield http


@asynccontextmanager
async def session_for(app: AppFactory, token: str | None, **headers: str) -> AsyncIterator[Any]:
    """An MCP client speaking to the app in-process, over real HTTP semantics."""
    async with (
        http_for(app, token, **headers) as http,
        streamable_http_client(f"{BASE_URL}/mcp", http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        yield session


async def call(session: Any, tool: str, arguments: dict[str, Any]) -> Any:
    return await session.call_tool(tool, arguments)


async def raw_tools_list(app: AppFactory, token: str | None) -> Any:
    """A bare JSON-RPC POST, to observe the transport's own answer."""
    async with http_for(app, token) as http:
        return await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )


# --- health ------------------------------------------------------------


async def test_health_and_readiness_need_no_credential(app: AppFactory) -> None:
    async with http_for(app, None) as http:
        health = await http.get("/health")
        ready = await http.get("/ready")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


# --- authentication ----------------------------------------------------


async def test_an_uncredentialled_call_is_refused_at_the_transport(app: AppFactory) -> None:
    """It never reaches the tool dispatcher at all."""
    assert (await raw_tools_list(app, None)).status_code == 401


async def test_a_forged_token_is_refused(app: AppFactory) -> None:
    assert (await raw_tools_list(app, "cfm_not_a_real_token")).status_code == 401


async def test_rotation_replaces_one_person_and_leaves_the_other_working(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    alice_old = await tokens.issue(ALICE, "laptop")
    bob = await tokens.issue(BOB, "laptop")
    alice_new = await tokens.rotate(ALICE, "replacement")

    async with session_for(app, alice_new.token) as session:
        assert (await call(session, "list_channels", {})).structured_content["channel_count"] == 1
    async with session_for(app, bob.token) as session:
        assert (await call(session, "list_channels", {})).structured_content["channel_count"] == 2

    assert (await raw_tools_list(app, alice_old.token)).status_code == 401


# --- the tool surface --------------------------------------------------


async def test_the_advertised_schema_has_no_identity_field(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    issued = await tokens.issue(ALICE)
    async with session_for(app, issued.token) as session:
        tools = (await session.list_tools()).tools
    assert {t.name for t in tools} == {"search_messages", "thread_context", "list_channels"}
    for tool in tools:
        properties = set(tool.input_schema.get("properties", {}))
        assert not properties & {"viewer", "person_id", "user_id", "channel_ids", "acl"}


async def test_listing_reflects_each_person_s_own_access(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    alice = await tokens.issue(ALICE)
    bob = await tokens.issue(BOB)
    async with session_for(app, alice.token) as session:
        alice_channels = (await call(session, "list_channels", {})).structured_content
    async with session_for(app, bob.token) as session:
        bob_channels = (await call(session, "list_channels", {})).structured_content

    assert [c["channel_id"] for c in alice_channels["channels"]] == [str(GENERAL)]
    assert [c["channel_id"] for c in bob_channels["channels"]] == [
        str(GENERAL),
        str(LEADERSHIP),
    ]


async def test_search_returns_only_the_viewer_s_channels(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    alice = await tokens.issue(ALICE)
    bob = await tokens.issue(BOB)
    async with session_for(app, alice.token) as session:
        alice_hits = (
            await call(session, "search_messages", {"query": PRIVATE_TERM})
        ).structured_content
    async with session_for(app, bob.token) as session:
        bob_hits = (
            await call(session, "search_messages", {"query": PRIVATE_TERM})
        ).structured_content

    assert alice_hits["status"] == "ok"
    assert alice_hits["result_count"] == 0
    assert bob_hits["result_count"] > 0
    assert {r["channel_id"] for r in bob_hits["results"]} == {str(LEADERSHIP)}


async def test_no_matches_is_an_empty_success_not_a_failure(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    alice = await tokens.issue(ALICE)
    async with session_for(app, alice.token) as session:
        result = await call(session, "search_messages", {"query": "quetzalcoatl"})
    assert result.is_error is not True
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["result_count"] == 0


async def test_a_forbidden_channel_looks_exactly_like_a_missing_one(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    alice = await tokens.issue(ALICE)
    async with session_for(app, alice.token) as session:
        forbidden = await call(
            session,
            "search_messages",
            {"query": PRIVATE_TERM, "channel_id": str(LEADERSHIP)},
        )
        missing = await call(
            session,
            "search_messages",
            {"query": PRIVATE_TERM, "channel_id": "999999999999"},
        )
    assert forbidden.structured_content == missing.structured_content
    assert forbidden.structured_content["result_count"] == 0


async def test_context_is_returned_for_a_readable_message(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    alice = await tokens.issue(ALICE)
    async with session_for(app, alice.token) as session:
        result = await call(
            session, "thread_context", {"message_id": str(GENERAL_MESSAGE_ID)}
        )
    body = result.structured_content
    assert body["message_count"] >= 1
    assert body["messages"][0]["citation"]["url"] == (
        f"https://discord.com/channels/{GUILD}/{GENERAL}/{GENERAL_MESSAGE_ID}"
    )


async def test_context_reports_the_discord_author_id_not_the_person_row(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    """Regression: the author id is the platform account a client can mention.

    The store once wrapped the internal person row id as the Discord id, so
    every context message named an account that does not exist.
    """
    alice = await tokens.issue(ALICE)
    async with session_for(app, alice.token) as session:
        result = await call(
            session, "thread_context", {"message_id": str(GENERAL_MESSAGE_ID)}
        )
    authors = {m["author_id"] for m in result.structured_content["messages"]}
    assert authors == {str(AUTHOR_DISCORD_ID)}


async def test_context_for_a_private_message_is_refused_without_revealing_it(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    alice = await tokens.issue(ALICE)
    async with session_for(app, alice.token) as session:
        forbidden = await call(
            session, "thread_context", {"message_id": str(LEADERSHIP_MESSAGE_ID)}
        )
        missing = await call(
            session, "thread_context", {"message_id": str(MISSING_MESSAGE_ID)}
        )

    assert forbidden.is_error is True
    assert missing.is_error is True
    forbidden_text = [c.text for c in forbidden.content]
    assert forbidden_text == [c.text for c in missing.content]
    assert UNAVAILABLE in forbidden_text[0]
    assert PRIVATE_TERM not in forbidden_text[0]


# --- the impersonation attempt ------------------------------------------


async def test_alice_cannot_retrieve_as_bob_by_any_means(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    """Every lever the transport offers, aimed at Bob's private channel.

    Tool arguments, headers the framework might honour, query parameters on
    the endpoint itself. Bob's own result is fetched first so the assertion
    compares against what a successful impersonation would have produced,
    rather than against an empty set that might be empty for another reason.
    """
    alice = await tokens.issue(ALICE)
    bob = await tokens.issue(BOB)

    async with session_for(app, bob.token) as session:
        bob_sees = (
            await call(session, "search_messages", {"query": PRIVATE_TERM})
        ).structured_content
    assert bob_sees["result_count"] > 0, "the private channel must be findable by Bob"

    attempts: list[dict[str, Any]] = [
        {"query": PRIVATE_TERM},
        {"query": PRIVATE_TERM, "viewer": str(BOB)},
        {"query": PRIVATE_TERM, "viewer_id": BOB.platform_user_id},
        {"query": PRIVATE_TERM, "person": str(BOB)},
        {"query": PRIVATE_TERM, "person_id": BOB.platform_user_id},
        {"query": PRIVATE_TERM, "user_id": BOB.platform_user_id},
        {"query": PRIVATE_TERM, "on_behalf_of": BOB.platform_user_id},
        {"query": PRIVATE_TERM, "channel_ids": [LEADERSHIP]},
        {"query": PRIVATE_TERM, "visible_channels": [LEADERSHIP]},
        {"query": PRIVATE_TERM, "acl": {"channels": [LEADERSHIP]}},
        {"query": PRIVATE_TERM, "channel_id": str(LEADERSHIP)},
    ]

    async with session_for(app, alice.token) as session:
        for arguments in attempts:
            body = (await call(session, "search_messages", arguments)).structured_content
            assert body["result_count"] == 0, f"leaked via arguments {arguments}"

    forged_headers = {
        "X-Viewer": str(BOB.platform_user_id),
        "X-On-Behalf-Of": str(BOB.platform_user_id),
        "X-Person-Id": str(BOB.platform_user_id),
        "X-Discord-User-Id": str(BOB.platform_user_id),
        "X-Forwarded-User": str(BOB.platform_user_id),
        "X-Impersonate": str(BOB),
    }
    async with session_for(app, alice.token, **forged_headers) as session:
        body = (
            await call(session, "search_messages", {"query": PRIVATE_TERM})
        ).structured_content
        assert body["result_count"] == 0, "leaked via headers"

        context = await call(
            session, "thread_context", {"message_id": str(LEADERSHIP_MESSAGE_ID)}
        )
        assert context.is_error is True, "leaked via thread_context"

        channels = (await call(session, "list_channels", {})).structured_content
        assert [c["channel_id"] for c in channels["channels"]] == [str(GENERAL)]


async def test_bob_s_token_in_alice_s_query_string_does_not_help(
    app: AppFactory, tokens: PostgresTokenStore
) -> None:
    """A credential in the URL is not a credential.

    Only the Authorization header is consulted, so a token pasted into a
    query string authenticates nothing -- which also keeps tokens out of
    proxy access logs.
    """
    alice = await tokens.issue(ALICE)
    bob = await tokens.issue(BOB)
    async with http_for(app, alice.token) as http, streamable_http_client(
        f"{BASE_URL}/mcp?token={bob.token}&viewer={BOB.platform_user_id}",
        http_client=http,
    ) as streams, ClientSession(streams[0], streams[1]) as session:
        await session.initialize()
        body = (
            await session.call_tool("search_messages", {"query": PRIVATE_TERM})
        ).structured_content
    assert body["result_count"] == 0


# --- deletion ----------------------------------------------------------


async def test_tombstoned_content_stops_being_returned(
    app: AppFactory, corpus: AsyncEngine, tokens: PostgresTokenStore
) -> None:
    """Deleted content must disappear from the served surface, not just the
    repository."""
    bob = await tokens.issue(BOB)
    async with session_for(app, bob.token) as session:
        before = (
            await call(session, "search_messages", {"query": PRIVATE_TERM})
        ).structured_content
    assert before["result_count"] > 0

    async with corpus.begin() as conn:
        await conn.execute(
            text("UPDATE conversation_window SET deleted_at = now() WHERE channel_id = :c"),
            {"c": LEADERSHIP},
        )
        await conn.execute(
            text("UPDATE message SET deleted_at = now() WHERE channel_id = :c"),
            {"c": LEADERSHIP},
        )

    async with session_for(app, bob.token) as session:
        after = (
            await call(session, "search_messages", {"query": PRIVATE_TERM})
        ).structured_content
        context = await call(
            session, "thread_context", {"message_id": str(LEADERSHIP_MESSAGE_ID)}
        )
    assert after["result_count"] == 0
    assert context.is_error is True
