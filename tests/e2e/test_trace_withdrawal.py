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

An admin opt-out withdraws the traces of the person's own questions too: the
console, built by its entrypoint, marks them, and one pass of ingest's
withdrawal sweep searches Langfuse by the person's platform id for traces the
index never recorded and deletes both, never another app's or environment's.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.domain.identity import PersonRef
from chatmemory.entrypoints import admin
from tests.e2e.conftest import COFFEE, DEFAULT_CORPUS
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.process import e2e_settings, start
from tests.e2e.harness.web import NetworkSeal

LANGFUSE = "langfuse.e2e.test"


def _langfuse(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        return httpx.Response(207, json={"successes": [], "errors": []})
    return httpx.Response(200, json={"message": "Traces deleted"})


class FakeLangfuse:
    """Accepts exports, lists `held` traces by user, answers DELETE with `delete_status`."""

    def __init__(self) -> None:
        self.held: list[dict[str, str]] = []
        self.delete_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(207, json={"successes": [], "errors": []})
        if request.method == "GET":
            user = request.url.params["userId"]
            rows = [r for r in self.held if r["userId"] == user]
            return httpx.Response(200, json={"data": rows, "meta": {"page": 1, "totalPages": 1}})
        if self.delete_status != 200:
            return httpx.Response(self.delete_status, json={"message": "Invalid request data"})
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


def _deleted(bot: E2EBot) -> list[list[str]]:
    return [
        json.loads(r.content)["traceIds"]
        for r in bot.web.calls
        if r.method == "DELETE" and r.url.host == LANGFUSE
    ]


async def _admin_opt_out(database_url: str, person: PersonRef) -> int:
    """What `POST /api/optouts` runs, over the services the admin entrypoint builds."""
    console = admin.build({"DATABASE_URL": database_url})
    try:
        report = await console.services.optouts.opt_out(person, reason="console:e2e")
    finally:
        await console.engine.dispose()
    return report.traces


async def _ask_and_opt_out(
    bot: E2EBot, langfuse: FakeLangfuse, database_url: str
) -> tuple[str, str]:
    bea = bot.person("Bea")
    await bot.channel("general", bea).say("when will the coffee maker be fixed?")
    [export] = [r for r in bot.web.calls if r.method == "POST" and r.url.host == LANGFUSE]
    [asked] = [e["body"]["id"] for e in json.loads(export.content)["batch"]]
    user = str(bea.id)
    langfuse.held = [
        {"id": asked, "name": "fixed", "environment": "production", "userId": user},
        # Exported before the index recorded askers: only the search finds it.
        {"id": "legacy-bea", "name": "loop", "environment": "production", "userId": user},
        # Same user id, not ours to delete.
        {"id": "staging-bea", "name": "fixed", "environment": "staging", "userId": user},
        {"id": "other-app-bea", "name": "checkout", "environment": "production",
         "userId": user},
    ]
    assert await _admin_opt_out(database_url, PersonRef("discord", bea.id)) == 1
    return asked, user


async def test_an_admin_opt_out_withdraws_the_persons_asked_traces(
    traced: E2EBot, e2e_database_url: str
) -> None:
    bot = traced
    langfuse = FakeLangfuse()
    bot.web.script(LANGFUSE, langfuse)
    asked, user = await _ask_and_opt_out(bot, langfuse, e2e_database_url)

    await bot.ingest.sweep_traces()

    [search] = [r for r in bot.web.calls if r.method == "GET" and r.url.host == LANGFUSE]
    assert search.url.params["userId"] == user
    assert search.url.params["environment"] == "production"
    assert [sorted(ids) for ids in _deleted(bot)] == [sorted([asked, "legacy-bea"])]
    assert bot.seal.refused == []
    assert await _pending(bot.engine) == 0


async def test_a_refused_deletion_stays_pending_until_langfuse_accepts(
    traced: E2EBot, e2e_database_url: str
) -> None:
    """A Langfuse that answers the DELETE with a 400 deleted nothing."""
    bot = traced
    langfuse = FakeLangfuse()
    langfuse.delete_status = 400
    bot.web.script(LANGFUSE, langfuse)
    asked, _ = await _ask_and_opt_out(bot, langfuse, e2e_database_url)

    await bot.ingest.sweep_traces()
    assert await _pending(bot.engine) == 2

    langfuse.delete_status = 200
    await bot.ingest.sweep_traces()

    assert sorted(_deleted(bot)[-1]) == sorted([asked, "legacy-bea"])
    assert await _pending(bot.engine) == 0
