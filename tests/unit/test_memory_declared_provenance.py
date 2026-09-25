"""Answers that read no channel, or read known ones, say so, and are remembered.

`Conversations.remember` refuses a turn whose answer leaves
`consulted_channels` as None ("provenance not established"). Two answer
services never set it, so their turns were silently never remembered and a
follow-up had nothing to refer back to: the capability description, and
obligations ("what do I need to do?"), both when something was outstanding and
when nothing was. Driven through the real `Conversations.remember`.
"""

from __future__ import annotations

from chatmemory.app.asks.obligations import NOTHING_OUTSTANDING, ObligationService
from chatmemory.app.self_description import SelfDescriptionAnswerService
from chatmemory.ports.memory import ConversationLocation, Recollection
from tests.unit.test_asks_obligations import store_with
from tests.unit.test_asks_support import BOB, OPEN_CHANNEL, FakeAskStore, ask, viewer
from tests.unit.test_conversation_memory import FakeMemoryStore, conversations
from tests.unit.test_reasoning_fixed import question

HERE = ConversationLocation("discord", 99, direct=True)


async def _remembered(answer: object) -> bool:
    memory = FakeMemoryStore()
    stored = await conversations(memory).remember(
        viewer(BOB, OPEN_CHANNEL),
        HERE,
        "q",
        answer,  # type: ignore[arg-type]
        informed_by=Recollection(),
    )
    return stored and len(memory.turns) == 1


async def test_the_capability_description_is_remembered() -> None:
    class Fallback:
        async def answer(self, q: object) -> object:
            raise AssertionError("self-description should answer this itself")

    front = SelfDescriptionAnswerService(Fallback())  # type: ignore[arg-type]
    answer = await front.answer(question(text="what can you do?"))

    assert answer.consulted_channels == frozenset()
    assert await _remembered(answer)


async def test_outstanding_obligations_are_remembered_with_their_channels() -> None:
    store = store_with(ask())
    answer = await ObligationService(store).what_i_need_to_do(viewer(BOB, OPEN_CHANNEL))

    assert answer.text != NOTHING_OUTSTANDING
    assert answer.consulted_channels == frozenset({OPEN_CHANNEL})
    assert await _remembered(answer)


async def test_nothing_outstanding_is_remembered_against_the_channels_searched() -> None:
    answer = await ObligationService(FakeAskStore()).what_i_need_to_do(
        viewer(BOB, OPEN_CHANNEL)
    )

    assert answer.text == NOTHING_OUTSTANDING
    assert answer.consulted_channels == frozenset({OPEN_CHANNEL})
    assert await _remembered(answer)
