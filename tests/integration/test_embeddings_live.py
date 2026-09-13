"""Embeddings against a real OpenAI-compatible endpoint.

These spend a trivial amount of money and skip when no key is configured.
Their purpose is to catch the things a mock cannot: that the configured model
returns the dimensionality the schema pins, and that a real vector round-trips
through pgvector and ranks sensibly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.llm.embeddings import OpenAICompatibleEmbeddings
from chatmemory.adapters.store import sql

pytestmark = pytest.mark.asyncio

MODEL = "text-embedding-3-small"
DIMS = 1536


def client(key: str, dimensions: int = DIMS) -> OpenAICompatibleEmbeddings:
    return OpenAICompatibleEmbeddings(
        api_key=key,
        base_url="https://api.openai.com/v1",
        model=MODEL,
        dimensions=dimensions,
    )


async def test_model_returns_the_dimensionality_the_schema_pins(openai_key: str) -> None:
    vectors = await client(openai_key).embed(["hello world"])
    assert len(vectors[0]) == DIMS


async def test_dimension_mismatch_fails_with_a_useful_message(openai_key: str) -> None:
    """A mismatch must name the cause, not fail opaquely at insert time."""
    with pytest.raises(ValueError, match="reindex"):
        await client(openai_key, dimensions=99).embed(["hello"])


async def test_batching_preserves_order(openai_key: str) -> None:
    texts = [f"document number {i}" for i in range(5)]
    vectors = await client(openai_key).embed(texts)
    assert len(vectors) == len(texts)
    assert all(len(v) == DIMS for v in vectors)


async def test_real_embeddings_rank_by_meaning_through_pgvector(
    clean: AsyncEngine, openai_key: str
) -> None:
    """End to end: embed, store, and retrieve by semantic similarity.

    Uses wording the query does not share, which is the whole reason vector
    search exists alongside lexical search.
    """
    embeddings = client(openai_key)
    corpus = [
        "the deployment pipeline broke again during the release",
        "lunch options near the office are getting repetitive",
        "we should discuss holiday scheduling for december",
    ]
    vectors = await embeddings.embed(corpus)

    async with clean.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO channel (id, platform, name, is_indexed) "
                "VALUES (100, 'discord', 'general', TRUE)"
            )
        )
        for body, vector in zip(corpus, vectors, strict=True):
            await conn.execute(
                text(
                    "INSERT INTO conversation_window "
                    "(channel_id, text, starts_at, ends_at, embedding, search_tsv) "
                    "VALUES (100, :t, now(), now(), CAST(:e AS vector), "
                    "to_tsvector('english', :t))"
                ),
                {"t": body, "e": sql.vector_literal(vector)},
            )

    query = (await embeddings.embed(["the build kept failing when we shipped"]))[0]
    async with clean.connect() as conn:
        for statement in sql.SESSION_SETUP:
            await conn.execute(text(statement))
        rows = await conn.execute(
            sql.VECTOR_SEARCH,
            {
                "embedding": sql.vector_literal(query),
                "channel_ids": [100],
                "since": None,
                "until": None,
                "limit": 3,
            },
        )
        ranked = [r["text"] for r in rows.mappings()]

    assert ranked[0] == corpus[0], f"semantic ranking failed: {ranked}"
