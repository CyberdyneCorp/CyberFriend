"""The MCP surface against a real embedding endpoint.

The rest of the integration suite stands the embedder in, which means the
vector leg of the hybrid search runs but proves nothing. This test asks a
question that shares no words with the text it should find, so only a real
embedding can answer it -- and then checks that the permission predicate
still holds on the approximate scan, which is the leg where post-filtering
would silently under-return.

Skipped when no key is configured; it spends a few tenths of a cent when run.
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

from chatmemory.adapters.llm.embeddings import OpenAICompatibleEmbeddings
from chatmemory.adapters.store import sql
from chatmemory.adapters.store.postgres import HybridSearch
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.mcp.auth import Authenticator, PostgresTokenStore, ensure_token_schema
from chatmemory.mcp.server import build_app

pytestmark = pytest.mark.asyncio

GUILD = 7100
GENERAL, LEADERSHIP = 110, 310
ALICE = PersonRef("discord", 2001)
BOB = PersonRef("discord", 2002)
T0 = datetime(2026, 9, 13, tzinfo=UTC)
BASE_URL = "http://127.0.0.1"

MODEL = "text-embedding-3-small"
DIMS = 1536

# Deliberately share no content words with the question asked below, so a
# lexical match cannot account for a hit.
OPEN_TEXTS = [
    "the release train ships every friday afternoon",
    "staging was red overnight, rolled back the container image",
    "who owns the terraform module for the ingress",
]
PRIVATE_TEXTS = [
    "salary bands for the senior engineering ladder were revised",
    "the promotion committee meets before the quarterly cycle closes",
    "budget for headcount next year is approved at eleven",
]

QUESTION = "how does this company decide what people get paid"

AppFactory = Callable[[], Starlette]


class FakeAcl:
    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        readable = {
            ALICE: frozenset({ChannelRef("discord", GENERAL)}),
            BOB: frozenset({ChannelRef("discord", GENERAL), ChannelRef("discord", LEADERSHIP)}),
        }
        return Viewer(person=person, visible_channels=readable.get(person, frozenset()))


async def seed_with_vectors(
    engine: AsyncEngine, embeddings: OpenAICompatibleEmbeddings
) -> None:
    texts = OPEN_TEXTS + PRIVATE_TEXTS
    vectors: Sequence[Sequence[float]] = await embeddings.embed(texts)
    rows = [
        (GENERAL if i < len(OPEN_TEXTS) else LEADERSHIP, t, v)
        for i, (t, v) in enumerate(zip(texts, vectors, strict=True))
    ]
    async with engine.begin() as conn:
        for channel_id, name in ((GENERAL, "general"), (LEADERSHIP, "leadership")):
            await conn.execute(
                text(
                    "INSERT INTO channel (id, platform, name, is_indexed) "
                    "VALUES (:id, 'discord', :n, TRUE)"
                ),
                {"id": channel_id, "n": name},
            )
        for i, (channel_id, body, vector) in enumerate(rows):
            await conn.execute(
                text(
                    "INSERT INTO conversation_window "
                    "(channel_id, text, starts_at, ends_at, search_tsv, embedding) "
                    "VALUES (:c, :t, :s, :e, to_tsvector('english', :t), "
                    "CAST(:v AS vector))"
                ),
                {
                    "c": channel_id,
                    "t": body,
                    "s": T0 + timedelta(minutes=i),
                    "e": T0 + timedelta(minutes=i + 1),
                    "v": sql.vector_literal(vector),
                },
            )


@pytest_asyncio.fixture
async def live(clean: AsyncEngine, openai_key: str) -> AsyncIterator[AsyncEngine]:
    await ensure_token_schema(clean)
    async with clean.begin() as conn:
        await conn.execute(text("DELETE FROM mcp_token"))
    await seed_with_vectors(clean, embeddings(openai_key))
    yield clean


def embeddings(key: str) -> OpenAICompatibleEmbeddings:
    return OpenAICompatibleEmbeddings(
        api_key=key,
        base_url="https://api.openai.com/v1",
        model=MODEL,
        dimensions=DIMS,
    )


@asynccontextmanager
async def session_for(app: AppFactory, token: str) -> AsyncIterator[Any]:
    application = app()
    async with (
        application.router.lifespan_context(application),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=application),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
        ) as http,
        streamable_http_client(f"{BASE_URL}/mcp", http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        yield session


async def test_a_paraphrased_question_finds_only_what_the_viewer_may_read(
    live: AsyncEngine, openai_key: str
) -> None:
    tokens = PostgresTokenStore(live)
    alice = await tokens.issue(ALICE)
    bob = await tokens.issue(BOB)

    def build() -> Starlette:
        return build_app(
            search=HybridSearch(live, embeddings(openai_key)),
            authenticator=Authenticator(tokens=tokens, acl=FakeAcl()),
            guild_id=GUILD,
        )

    async with session_for(build, bob.token) as session:
        bob_sees = (
            await session.call_tool("search_messages", {"query": QUESTION})
        ).structured_content

    async with session_for(build, alice.token) as session:
        alice_sees = (
            await session.call_tool("search_messages", {"query": QUESTION})
        ).structured_content

    # The question shares no content words with the answer, so a hit here is
    # the embedding endpoint doing its job rather than the tsvector index.
    assert bob_sees["result_count"] > 0
    assert str(LEADERSHIP) in {r["channel_id"] for r in bob_sees["results"]}
    top = bob_sees["results"][0]
    assert top["relevance_source"] == "fused_rrf"

    # Same question, same corpus, narrower person: the private channel is not
    # merely ranked lower, it is absent.
    assert str(LEADERSHIP) not in {r["channel_id"] for r in alice_sees["results"]}
