"""What reaches Langfuse from the running bot: named, tagged, and quoting nothing.

Drives the bot the composition root builds, with tracing configured against
FakeWeb, and reads the ingestion batch it sent. The trace is named by its
feature and tagged for this application, the capabilities reply is traced now
that the tracer sits at the outermost answer service, catch-up and said-by --
answered before that chain -- are traced through the same tracer, the evidence
behind an
answer leaves as references only, and an opted-out person is still never
exported.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from chatmemory.adapters.tracing.langfuse import APP_TAG
from chatmemory.app.reasoning import features
from chatmemory.domain.identity import PersonRef
from tests.e2e.conftest import COFFEE
from tests.e2e.harness.conversation import PLATFORM, E2EBot
from tests.e2e.harness.model import MODEL
from tests.e2e.harness.process import NOW
from tests.e2e.test_trace_withdrawal import LANGFUSE, _admin_opt_out, traced

__all__ = ["traced"]

REFERENCE_KEYS = {"window_id", "channel", "source_system", "score"}


def _exports(bot: E2EBot) -> list[dict[str, Any]]:
    return [
        event["body"]
        for request in bot.web.calls
        if request.method == "POST" and request.url.host == LANGFUSE
        for event in json.loads(request.content)["batch"]
        if event["type"] == "trace-create"
    ]


async def test_a_corpus_answer_is_named_tagged_and_quotes_no_evidence(traced: E2EBot) -> None:
    bot = traced
    turn = await bot.channel("general", bot.person("Bea")).say(
        "when will the coffee maker be fixed?"
    )
    assert COFFEE in turn.text

    [body] = _exports(bot)
    assert body["name"] in {features.CORPUS_FIXED, features.CORPUS_LOOP}
    assert body["environment"] == "production"
    assert APP_TAG in body["tags"]
    assert f"feature:{body['name']}" in body["tags"]
    assert "lang:en" in body["tags"]

    metadata = body["metadata"]
    assert metadata["evidence"], "the run held evidence; its references must be there"
    assert all(set(item) == REFERENCE_KEYS for item in metadata["evidence"])
    assert all("excerpt" not in item for item in metadata["citations"])
    assert COFFEE not in json.dumps(metadata), "the evidence text reached Langfuse"


async def test_a_capabilities_question_is_traced_as_capabilities(traced: E2EBot) -> None:
    bot = traced
    await bot.dm(bot.person("Bea")).say("what can you do?")

    [body] = _exports(bot)
    assert body["name"] == features.CAPABILITIES
    assert APP_TAG in body["tags"]


async def test_a_catch_up_is_traced_as_corpus_catchup(traced: E2EBot) -> None:
    """Answered by the ask service before the answer chain, and traced anyway."""
    bot = traced
    turn = await bot.dm(bot.person("Bea")).say("what did I miss in #general?")
    assert turn.searched, "catch-up did not search the channel"

    [body] = _exports(bot)
    assert body["name"] == features.CORPUS_CATCHUP
    assert APP_TAG in body["tags"]
    assert f"feature:{features.CORPUS_CATCHUP}" in body["tags"]
    metadata = body["metadata"]
    assert metadata["evidence"], "the summary drew on evidence; its references must be there"
    assert all(set(item) == REFERENCE_KEYS for item in metadata["evidence"])
    assert COFFEE not in json.dumps(metadata), "the evidence text reached Langfuse"


async def test_a_said_by_answer_is_traced_as_corpus_said_by(traced: E2EBot) -> None:
    bot = traced
    leo = bot.person("Leo")
    line = "the deploy is scheduled for friday night"
    await bot.seed_conversation(
        "general", [(PersonRef(PLATFORM, leo.id), "Leo", line, NOW - timedelta(days=1))]
    )

    turn = await bot.dm(bot.person("Bea")).say("what did Leo say about the deploy?")
    assert line in turn.text

    [body] = _exports(bot)
    assert body["name"] == features.CORPUS_SAID_BY
    assert APP_TAG in body["tags"]
    decisions = body["metadata"]["decisions"]
    assert any(d["name"] == "said_by" and d["outcome"] == "resolved" for d in decisions)
    assert line not in json.dumps(body["metadata"]), "the evidence text reached Langfuse"


async def test_an_opted_out_person_is_not_exported(
    traced: E2EBot, e2e_database_url: str
) -> None:
    bot = traced
    bea = bot.person("Bea")
    await _admin_opt_out(e2e_database_url, PersonRef("discord", bea.id))

    await bot.dm(bea).say("what can you do?")

    assert _exports(bot) == []


async def test_a_corpus_answer_carries_a_generation_per_model_call(traced: E2EBot) -> None:
    """Tokens and model per call ride in the trace's own batch, so Langfuse can price it."""
    bot = traced
    await bot.channel("general", bot.person("Bea")).say("when will the coffee maker be fixed?")

    [request] = [r for r in bot.web.calls if r.method == "POST" and r.url.host == LANGFUSE]
    events = json.loads(request.content)["batch"]
    [trace] = [e["body"] for e in events if e["type"] == "trace-create"]
    generations = [e["body"] for e in events if e["type"] == "generation-create"]
    assert generations, "the answer was synthesised; its model call must be exported"
    assert all(g["traceId"] == trace["id"] for g in generations)
    assert {g.get("model") for g in generations} == {MODEL}
    assert all(set(g["usageDetails"]) == {"input", "output"} for g in generations)
    assert all(g["usageDetails"]["output"] > 0 for g in generations)
    assert "synthesis" in {g["name"] for g in generations}
    assert COFFEE not in json.dumps(generations)
