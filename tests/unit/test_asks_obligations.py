"""Answering obligation questions from records rather than from resemblance."""

from __future__ import annotations

from datetime import timedelta

from chatmemory.app.asks.model import AskKind, AskPolicy, AskStatus, to_person
from chatmemory.app.asks.obligations import (
    NOTHING_OUTSTANDING,
    ObligationService,
    discord_message_url,
)
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    CARA,
    OPEN_CHANNEL,
    PRIVATE_CHANNEL,
    T0,
    FakeAskStore,
    ask,
    message,
    viewer,
)

YESTERDAY = T0 - timedelta(days=1)
LAST_MONTH = T0 - timedelta(days=30)


def store_with(*asks: object) -> FakeAskStore:
    store = FakeAskStore()
    for item in asks:
        store.asks[item.key] = item  # type: ignore[attr-defined]
        store.messages[item.source_message_id] = message(  # type: ignore[attr-defined]
            item.source_message_id,  # type: ignore[attr-defined]
            item.requester,  # type: ignore[attr-defined]
            "can you review the migration?",
        )
    return store


async def test_what_was_asked_of_me_returns_asks_addressed_to_me() -> None:
    store = store_with(
        ask(source_message_id=10, requester=ALICE, addressee=to_person(BOB)),
        ask(source_message_id=11, requester=ALICE, addressee=to_person(CARA)),
    )
    answer = await ObligationService(store).asked_of_me(viewer(BOB, OPEN_CHANNEL))
    assert len(answer.citations) == 1
    assert answer.citations[0].message_id == 10


async def test_the_period_is_applied() -> None:
    store = store_with(
        ask(source_message_id=10, asked_at=T0),
        ask(source_message_id=11, asked_at=LAST_MONTH),
    )
    answer = await ObligationService(store).asked_of_me(
        viewer(BOB, OPEN_CHANNEL), since=YESTERDAY
    )
    assert [c.message_id for c in answer.citations] == [10]


async def test_what_was_asked_of_me_excludes_my_own_commitments() -> None:
    """They are things I said, not things anybody asked of me."""
    store = store_with(
        ask(
            source_message_id=12,
            requester=BOB,
            addressee=to_person(BOB),
            kind=AskKind.COMMITMENT,
        )
    )
    answer = await ObligationService(store).asked_of_me(viewer(BOB, OPEN_CHANNEL))
    assert answer.text == NOTHING_OUTSTANDING


async def test_what_i_need_to_do_includes_my_commitments() -> None:
    store = store_with(
        ask(source_message_id=10, requester=ALICE, addressee=to_person(BOB)),
        ask(
            source_message_id=12,
            requester=BOB,
            addressee=to_person(BOB),
            kind=AskKind.COMMITMENT,
            text="push the fix tonight",
        ),
    )
    answer = await ObligationService(store).what_i_need_to_do(viewer(BOB, OPEN_CHANNEL))
    assert len(answer.citations) == 2


async def test_nothing_outstanding_is_a_successful_answer() -> None:
    """Not an abstention: "nothing" is the true answer to the question."""
    answer = await ObligationService(FakeAskStore()).what_i_need_to_do(
        viewer(BOB, OPEN_CHANNEL)
    )
    assert answer.text == NOTHING_OUTSTANDING
    assert answer.abstained is False
    assert answer.citations == ()


async def test_every_reported_ask_carries_a_citation() -> None:
    store = store_with(ask(source_message_id=10), ask(source_message_id=11))
    answer = await ObligationService(
        store, message_url=discord_message_url(guild_id=5)
    ).what_i_need_to_do(viewer(BOB, OPEN_CHANNEL))

    assert len(answer.citations) == 2
    for index, citation in enumerate(answer.citations, start=1):
        assert citation.url.startswith("https://discord.com/channels/5/")
        assert citation.excerpt
        # Every reported line points at the citation that backs it.
        assert f"[{index}]" in answer.text


async def test_the_citation_quotes_the_source_not_the_extraction() -> None:
    """A citation exists so the model's claim can be checked against the words."""
    store = store_with(ask(source_message_id=10, text="review the migration"))
    answer = await ObligationService(store).what_i_need_to_do(viewer(BOB, OPEN_CHANNEL))
    assert answer.citations[0].excerpt == "can you review the migration?"


async def test_asks_in_unreadable_channels_are_not_returned() -> None:
    store = store_with(ask(source_message_id=10, channel=PRIVATE_CHANNEL))
    answer = await ObligationService(store).what_i_need_to_do(viewer(BOB, OPEN_CHANNEL))
    assert answer.text == NOTHING_OUTSTANDING


async def test_asks_in_unreadable_channels_are_not_counted() -> None:
    """A count that includes them reports the conversation exists."""
    store = store_with(
        ask(source_message_id=10, channel=PRIVATE_CHANNEL),
        ask(source_message_id=11, channel=OPEN_CHANNEL),
    )
    service = ObligationService(store)
    assert await service.outstanding_count(viewer(BOB, OPEN_CHANNEL)) == 1
    assert await service.outstanding_count(viewer(BOB, OPEN_CHANNEL, PRIVATE_CHANNEL)) == 2


async def test_a_viewer_with_no_channels_sees_nothing() -> None:
    store = store_with(ask(source_message_id=10))
    assert await ObligationService(store).outstanding_count(viewer(BOB)) == 0


async def test_low_confidence_asks_are_not_presented_as_obligations() -> None:
    store = store_with(ask(source_message_id=10, confidence=0.3))
    answer = await ObligationService(
        store, policy=AskPolicy(min_confidence=0.6)
    ).what_i_need_to_do(viewer(BOB, OPEN_CHANNEL))
    assert answer.text == NOTHING_OUTSTANDING


async def test_answered_asks_are_not_reported() -> None:
    store = store_with(ask(source_message_id=10, status=AskStatus.ANSWERED))
    answer = await ObligationService(store).what_i_need_to_do(viewer(BOB, OPEN_CHANNEL))
    assert answer.text == NOTHING_OUTSTANDING


async def test_a_stale_ask_is_still_reported_and_labelled() -> None:
    """Ageing hides nothing; it is exactly what the person is asking about."""
    store = store_with(ask(source_message_id=10, status=AskStatus.STALE))
    answer = await ObligationService(store).what_i_need_to_do(viewer(BOB, OPEN_CHANNEL))
    assert len(answer.citations) == 1
    assert "(stale)" in answer.text


async def test_an_ask_whose_source_was_deleted_is_not_reported() -> None:
    store = store_with(ask(source_message_id=10))
    store.deleted.add(10)
    answer = await ObligationService(store).what_i_need_to_do(viewer(BOB, OPEN_CHANNEL))
    assert answer.text == NOTHING_OUTSTANDING
    assert await ObligationService(store).outstanding_count(viewer(BOB, OPEN_CHANNEL)) == 0
