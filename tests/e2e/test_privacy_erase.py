"""`/privacy` -> [Delete everything...] through the assembled process and real Postgres.

The person presses [Delete everything...], reads what goes and what is kept,
picks one of the two buttons and types the word. Nobody else can press any
of it, and a wrong word deletes nothing. After the word, no row of theirs is
left in any personal table, the trace of their question is deleted from
Langfuse by ingest's withdrawal sweep (through FakeWeb), and the reply gives
counts. Re-capturing their old messages brings nothing back; with the first
button a message they send afterwards is archived, with the second it is not.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import PersonRef
from tests.e2e.conftest import DEFAULT_CORPUS
from tests.e2e.harness.conversation import Conversation, E2EBot, Turn
from tests.e2e.harness.discord_wire import Sent
from tests.e2e.harness.process import NOW, e2e_settings, start
from tests.e2e.harness.web import NetworkSeal

LANGFUSE = "langfuse.e2e.test"
EMAIL = "leo.erase@example.com"
QUESTION = "when will the coffee maker be fixed?"
OLD_CHATTER = "shipping the release tonight"

#: Every table the design promises holds no row of an erased person, by SQL
#: over their person id (`:i`) or platform id (`:u`).
PERSONAL = {
    "message": "SELECT count(*) FROM message WHERE author_person_id = :i",
    "message_media": (
        "SELECT count(*) FROM message_media mm JOIN message m ON m.id = mm.message_id "
        "WHERE m.author_person_id = :i"
    ),
    "media_usage": "SELECT count(*) FROM media_usage WHERE person_id = :i",
    "scheduled_task": "SELECT count(*) FROM scheduled_task WHERE person_id = :i",
    "person_fact": "SELECT count(*) FROM person_fact WHERE person_id = :i",
    "conversation_turn": "SELECT count(*) FROM conversation_turn WHERE person_id = :i",
    "mcp_token": "SELECT count(*) FROM mcp_token WHERE platform_user_id = :u",
    "document_fetch": (
        "SELECT count(*) FROM document_fetch f JOIN message m ON m.id = f.message_id "
        "WHERE m.author_person_id = :i"
    ),
}


def _langfuse(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        return httpx.Response(207, json={"successes": [], "errors": []})
    if request.method == "GET":
        return httpx.Response(200, json={"data": [], "meta": {"page": 1, "totalPages": 1}})
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


async def _person_id(engine: AsyncEngine, user: int) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT person_id FROM person_platform_id WHERE platform_user_id = :u"),
            {"u": user},
        )
    return int(found)


async def _left(engine: AsyncEngine, user: int) -> dict[str, int]:
    person_id = await _person_id(engine, user)
    async with engine.connect() as conn:
        return {
            table: int(await conn.scalar(text(sql), {"i": person_id, "u": user}) or 0)
            for table, sql in PERSONAL.items()
        }


async def _opted_out(engine: AsyncEngine, user: int) -> bool:
    person_id = await _person_id(engine, user)
    async with engine.connect() as conn:
        return bool(
            await conn.scalar(
                text("SELECT count(*) FROM person_opt_out WHERE person_id = :i"), {"i": person_id}
            )
        )


def _exported(bot: E2EBot) -> list[str]:
    return [
        e["body"]["id"]
        for r in bot.web.calls
        if r.method == "POST" and r.url.host == LANGFUSE
        for e in json.loads(r.content)["batch"]
        if e["type"] == "trace-create"
    ]


def _deleted(bot: E2EBot) -> list[str]:
    return [
        trace
        for r in bot.web.calls
        if r.method == "DELETE" and r.url.host == LANGFUSE
        for trace in json.loads(r.content)["traceIds"]
    ]


async def _leo_with_history(bot: E2EBot) -> Conversation:
    """Leo has a fact, a traced question and an archived message."""
    leo = bot.person("Leo")
    await bot.dm(leo).say(f"my email is {EMAIL}")
    here = bot.channel("general", leo)
    await here.say(QUESTION)
    await bot.chatter("general", leo, OLD_CHATTER, at=NOW - timedelta(hours=1))
    assert _exported(bot), "the question was traced"
    return here


async def _choice(here: Conversation) -> Sent:
    """`/privacy`, then [Delete everything...]: the message with the two buttons."""
    shown = await here.slash("privacy")
    first = shown.sent[0]
    assert [label for label, _ in first.buttons] == ["Send me the details", "Delete everything…"]
    pressed = await here.press(first, "Delete everything…")
    [choice] = pressed.sent
    return choice


async def _confirm(here: Conversation, choice: Sent, button: str, word: str) -> Turn:
    opened = await here.press(choice, button)
    [modal] = opened.modals
    return await here.submit(modal, word)


async def test_delete_everything_and_keep_using_the_bot(traced: E2EBot) -> None:
    bot = traced
    here = await _leo_with_history(bot)
    leo = here.who
    [asked] = _exported(bot)

    choice = await _choice(here)

    assert choice.ephemeral
    assert "It can't be undone." in choice.text
    assert "New messages are archived as usual" in choice.text
    assert "Your name and preferences are cleared" in choice.text
    assert [label for label, _ in choice.buttons] == [
        "Delete everything",
        "Delete everything and stop archiving me",
    ]
    assert bot.discord.view_timeout(choice) == 120

    opened = await here.press(choice, "Delete everything")
    [modal] = opened.modals
    assert modal.labels == ("Type DELETE to confirm",)
    wrong = await here.submit(modal, "delete")
    assert "That wasn't DELETE, so nothing was deleted." in wrong.text
    assert dict(await bot.fact_rows(leo)) == {"email": EMAIL}

    done = await _confirm(here, choice, "Delete everything", "DELETE")

    assert all(s.ephemeral for s in done.sent)
    assert "Done. I deleted:" in done.text
    assert "Messages in channels you can read: 1," in done.text
    assert "Personal details: 1" in done.text
    assert "scheduled for deletion from the trace store: at least 1." in done.text
    assert "New messages are archived as usual" in done.text
    assert await _left(bot.engine, leo.id) == dict.fromkeys(PERSONAL, 0)
    assert not await _opted_out(bot.engine, leo.id)

    await bot.ingest.sweep_traces()
    assert asked in _deleted(bot)
    assert bot.seal.refused == []

    # A backfill re-reading the old message brings nothing back...
    await bot.chatter("general", leo, OLD_CHATTER, at=NOW - timedelta(hours=1))
    assert (await _left(bot.engine, leo.id))["message"] == 0
    # ...and a message sent now is archived as usual.
    await bot.chatter("general", leo, "back again", at=datetime.now(UTC) + timedelta(minutes=1))
    assert (await _left(bot.engine, leo.id))["message"] == 1


async def test_delete_everything_and_stop_archiving(traced: E2EBot) -> None:
    bot = traced
    dm = await _leo_with_history(bot)
    leo = dm.who
    [asked] = _exported(bot)
    here = bot.dm(leo)

    shown = await here.slash("privacy")
    [entry] = [s for s in shown.sent if s.buttons]
    assert [label for label, _ in entry.buttons] == ["Delete everything…"]
    [choice] = (await here.press(entry, "Delete everything…")).sent

    done = await _confirm(here, choice, "Delete everything and stop archiving me", "DELETE")

    assert "I've also stopped archiving your messages" in done.text
    assert await _left(bot.engine, leo.id) == dict.fromkeys(PERSONAL, 0)
    assert await _opted_out(bot.engine, leo.id)
    await bot.ingest.sweep_traces()
    assert asked in _deleted(bot)

    await bot.chatter("general", leo, "not archived", at=datetime.now(UTC) + timedelta(minutes=1))
    assert (await _left(bot.engine, leo.id))["message"] == 0


async def test_a_person_an_admin_opted_out_is_not_promised_archiving(traced: E2EBot) -> None:
    """Erasing never lifts an opt-out, so the choice offers one button and the
    reply says they stay out, not that new messages are archived."""
    bot = traced
    here = await _leo_with_history(bot)
    leo = here.who
    await OptOutService(PostgresRetentionStore(bot.engine)).opt_out(
        PersonRef("discord", leo.id), "console:e2e"
    )

    choice = await _choice(here)

    assert [label for label, _ in choice.buttons] == ["Delete everything"]
    assert "You're already opted out" in choice.text
    assert "archived as usual" not in choice.text

    done = await _confirm(here, choice, "Delete everything", "DELETE")

    assert "You stay opted out" in done.text
    assert "archived as usual" not in done.text
    assert await _opted_out(bot.engine, leo.id)
    await bot.chatter("general", leo, "not archived", at=datetime.now(UTC) + timedelta(minutes=1))
    assert (await _left(bot.engine, leo.id))["message"] == 0


async def test_nobody_else_can_press_and_the_buttons_stop_after_one_erasure(
    traced: E2EBot,
) -> None:
    bot = traced
    here = await _leo_with_history(bot)
    choice = await _choice(here)
    ana = Conversation(bot, bot.person("Ana"), here.channel)

    refused = await ana.press(choice, "Delete everything")

    [note] = refused.sent
    assert note.ephemeral and "Only the person who asked" in note.content
    assert refused.modals == ()
    assert dict(await bot.fact_rows(here.who)) == {"email": EMAIL}

    await _confirm(here, choice, "Delete everything", "DELETE")
    again = await here.press(choice, "Delete everything")
    assert again.modals == () and again.sent == ()


@pytest.mark.parametrize("word", ["APAGAR"])
async def test_a_portuguese_caller_types_apagar(traced: E2EBot, word: str) -> None:
    bot = traced
    bia = bot.person("Bia")
    here = bot.dm(bia)
    await here.say("meu email é bia@example.com")

    shown = await here.slash("privacy", locale="pt-BR")
    [entry] = [s for s in shown.sent if s.buttons]
    assert [label for label, _ in entry.buttons] == ["Apagar tudo…"]
    [choice] = (await here.press(entry, "Apagar tudo…")).sent
    assert "Não dá para desfazer." in choice.text
    opened = await here.press(choice, "Apagar tudo")
    [modal] = opened.modals
    assert modal.labels == ("Digite APAGAR para confirmar",)

    done = await here.submit(modal, word)

    assert done.text.startswith("Pronto. Eu apaguei:")
    assert await bot.fact_rows(bia) == []
