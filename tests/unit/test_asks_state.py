"""Open, answered, stale -- from events, never from judgement."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

from chatmemory.app.asks import state as state_module
from chatmemory.app.asks.model import AskPolicy, AskStatus, ClosedBy, to_group
from chatmemory.app.asks.state import (
    ACKNOWLEDGING_REACTIONS,
    AskStateService,
    Reaction,
    is_acknowledging,
    next_state,
)
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    CARA,
    T0,
    FakeAskStore,
    ask,
    message,
)

LATER = T0 + timedelta(minutes=5)
THREAD = 7


def threaded_ask() -> object:
    return ask(source_message_id=10, requester=ALICE, thread_id=THREAD)


async def test_an_ask_with_no_response_stays_open() -> None:
    transition = next_state(ask(), replies=[], reactions=[], now=LATER)
    assert transition.status is AskStatus.OPEN


async def test_a_reply_in_thread_by_the_addressee_answers_it() -> None:
    item = ask(source_message_id=10, thread_id=THREAD)
    reply = message(11, BOB, "done", at=LATER, thread_id=THREAD)
    transition = next_state(item, replies=[reply], now=LATER)
    assert transition.status is AskStatus.ANSWERED
    assert transition.by is ClosedBy.REPLY
    assert transition.at == LATER


async def test_a_direct_reply_to_the_asking_message_answers_it() -> None:
    item = ask(source_message_id=10)
    reply = message(11, BOB, "on it", at=LATER, reply_to_id=10)
    assert next_state(item, replies=[reply], now=LATER).status is AskStatus.ANSWERED


async def test_a_reply_from_somebody_else_does_not_answer_it() -> None:
    item = ask(source_message_id=10, thread_id=THREAD)
    reply = message(11, CARA, "i saw that too", at=LATER, thread_id=THREAD)
    assert next_state(item, replies=[reply], now=LATER).status is AskStatus.OPEN


async def test_unrelated_channel_chatter_does_not_answer_it() -> None:
    """People talk about other things in the same room."""
    item = ask(source_message_id=10, thread_id=THREAD)
    elsewhere = message(11, BOB, "lunch?", at=LATER)
    assert next_state(item, replies=[elsewhere], now=LATER).status is AskStatus.OPEN


async def test_a_reply_before_the_ask_does_not_answer_it() -> None:
    item = ask(source_message_id=10, asked_at=LATER, thread_id=THREAD)
    earlier = message(9, BOB, "morning", at=T0, thread_id=THREAD)
    assert next_state(item, replies=[earlier], now=LATER).status is AskStatus.OPEN


async def test_an_acknowledging_reaction_from_the_addressee_answers_it() -> None:
    transition = next_state(ask(), reactions=[Reaction(BOB, "✅", LATER)], now=LATER)
    assert transition.status is AskStatus.ANSWERED
    assert transition.by is ClosedBy.REACTION


async def test_a_reaction_from_somebody_else_does_not_answer_it() -> None:
    transition = next_state(ask(), reactions=[Reaction(CARA, "✅", LATER)], now=LATER)
    assert transition.status is AskStatus.OPEN


def test_eyes_is_not_an_acknowledgement() -> None:
    """It means somebody is looking, which is the opposite of finished."""
    assert not is_acknowledging("👀")
    assert is_acknowledging("✅")


async def test_ageing_marks_stale_and_never_closes() -> None:
    policy = AskPolicy(stale_after=timedelta(days=21))
    transition = next_state(ask(), now=T0 + timedelta(days=22), policy=policy)
    assert transition.status is AskStatus.STALE
    assert transition.status is not AskStatus.ANSWERED
    assert transition.by is None


async def test_a_stale_ask_is_still_outstanding() -> None:
    assert ask(status=AskStatus.STALE).is_outstanding


async def test_an_unattributed_ask_ages_but_is_never_answered() -> None:
    """Nobody is on the hook, so nothing anybody does can answer it."""
    item = ask(addressee=to_group("the platform team"))
    reply = message(11, BOB, "done", at=LATER, reply_to_id=item.source_message_id)
    assert next_state(item, replies=[reply], now=LATER).status is AskStatus.OPEN


async def test_only_acknowledging_reactions_are_stored() -> None:
    """This must not become a general record of who reacted to what."""
    store = FakeAskStore()
    service = AskStateService(store)
    assert await service.record_reaction(10, BOB, "👀", LATER) is False
    assert await service.record_reaction(10, BOB, "✅", LATER) is True
    assert len(store.reactions) == 1


async def test_refresh_applies_transitions_through_the_store() -> None:
    store = FakeAskStore()
    item = ask(source_message_id=10, thread_id=THREAD)
    store.asks[item.key] = item
    reply = message(11, BOB, "done", at=LATER, thread_id=THREAD)
    store.messages[11] = reply

    refreshed = await AskStateService(store).refresh(LATER)

    assert refreshed.answered_by_reply == 1
    assert store.asks[item.key].status is AskStatus.ANSWERED


def test_state_transitions_consult_no_model() -> None:
    """The rule this module exists for, asserted structurally.

    A model call here would make state expensive, inconsistent between runs and
    unauditable -- and an ask that "looks resolved" is exactly the one somebody
    is still waiting on.
    """
    source = Path(state_module.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "openai" not in imported
    for banned in ("extractor", "completion", "prompt", "llm"):
        assert banned not in source.lower().replace("no model", ""), banned


def test_the_acknowledging_set_is_explicit() -> None:
    assert ACKNOWLEDGING_REACTIONS
    assert "👀" not in ACKNOWLEDGING_REACTIONS
