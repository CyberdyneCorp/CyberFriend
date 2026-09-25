"""A deleted message's traces are withdrawn, and the harness can see it.

The bot exports the run of a question that cited a message; the author then
deletes it, and ingest asks Langfuse to delete every trace quoting it. The
deleter could only open its own httpx client over the real network, so in the
harness this deletion never reached FakeWeb: the sealed network refused it,
the trace stayed pending, and nothing an end-to-end test could see said so.
The harness now hands the deleter FakeWeb's transport. The ingest entrypoint
passes none, so production still deletes over httpx's default transport; what
this covers is the withdrawal logic and its wiring into ingest, not the
entrypoint's choice of transport.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.e2e.conftest import COFFEE, DEFAULT_CORPUS
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.process import e2e_settings, start
from tests.e2e.harness.web import NetworkSeal

LANGFUSE = "langfuse.e2e.test"


def _langfuse(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        return httpx.Response(207, json={"successes": [], "errors": []})
    return httpx.Response(200, json={"message": "Traces deleted"})


@pytest_asyncio.fixture
async def traced(
    clean: AsyncEngine, e2e_database_url: str, sealed_network: NetworkSeal
) -> AsyncIterator[E2EBot]:
    settings = e2e_settings(e2e_database_url).model_copy(
        update={
            "tracing_enabled": True,
            "langfuse_host": f"https://{LANGFUSE}",
            "langfuse_public_key": SecretStr("pk-e2e"),
            "langfuse_secret_key": SecretStr("sk-e2e"),
        }
    )
    e2e = await start(settings, clean, sealed_network)
    e2e.web.script(LANGFUSE, _langfuse)
    await e2e.seed_corpus(DEFAULT_CORPUS)
    try:
        yield e2e
    finally:
        federation = e2e.process.stack.federation
        if federation is not None:
            await federation.federation.aclose()


async def _pending(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT count(*) FROM trace_export "
                "WHERE deletion_requested_at IS NOT NULL AND deleted_at IS NULL"
            )
        )
    return int(found or 0)


async def test_deleting_a_quoted_message_deletes_its_trace_through_fakeweb(
    traced: E2EBot,
) -> None:
    bot = traced
    turn = await bot.channel("general", bot.person("Bea")).say(
        "when will the coffee maker be fixed?"
    )
    assert COFFEE in turn.text
    [export] = [r for r in bot.web.calls if r.method == "POST" and r.url.host == LANGFUSE]
    [trace_id] = [e["body"]["id"] for e in json.loads(export.content)["batch"]]

    await bot.delete("general", COFFEE)

    deletions = [r for r in bot.web.calls if r.method == "DELETE" and r.url.host == LANGFUSE]
    assert [json.loads(r.content)["traceIds"] for r in deletions] == [[trace_id]]
    assert bot.seal.refused == [], "the deletion went out over the real network"
    assert await _pending(bot.engine) == 0, "the trace is still waiting to be deleted"
