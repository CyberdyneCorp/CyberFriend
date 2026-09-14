"""The cost filter: which messages reach the extractor at all.

Two failure modes matter here and they pull in opposite directions. Letting
everything through turns extraction into a standing bill proportional to
traffic; letting too little through means obligations are never seen, and a
missing ask is invisible -- nothing in the system reports that it was skipped.
"""

from __future__ import annotations

from chatmemory.app.asks.candidates import CandidateFilter
from chatmemory.app.asks.model import AddresseeSignal
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    BOT,
    DM_CHANNEL,
    T0,
    message,
)


def test_a_mention_makes_a_message_a_candidate() -> None:
    found = CandidateFilter().candidates(
        [message(1, ALICE, "can you look at this?", mentions=frozenset({BOB}))]
    )
    assert [c.signal for c in found] == [AddresseeSignal.MENTION]


def test_a_reply_makes_a_message_a_candidate() -> None:
    parent = message(1, BOB, "migration is merged")
    reply = message(2, ALICE, "re-run it on staging please", reply_to_id=1)
    found = CandidateFilter().candidates([parent, reply])
    assert [c.signal for c in found] == [AddresseeSignal.REPLY]
    assert found[0].reply_parent == parent


def test_a_direct_message_to_the_bot_is_a_candidate() -> None:
    found = CandidateFilter(dm_channels=frozenset({DM_CHANNEL})).candidates(
        [message(1, ALICE, "remind me about the deploy steps", channel=DM_CHANNEL)]
    )
    assert [c.signal for c in found] == [AddresseeSignal.BOT_DM]


def test_second_person_address_is_a_candidate_without_a_mention() -> None:
    """The majority case: an obligation nobody was tagged in."""
    found = CandidateFilter().candidates([message(1, ALICE, "could you rerun the sync")])
    assert [c.signal for c in found] == [AddresseeSignal.SECOND_PERSON]


def test_group_address_is_a_candidate() -> None:
    found = CandidateFilter().candidates(
        [message(1, ALICE, "can someone on the platform team take the nightly build")]
    )
    assert [c.signal for c in found] == [AddresseeSignal.SECOND_PERSON]


def test_a_commitment_with_no_addressee_signal_is_still_a_candidate() -> None:
    """A promise is addressed to the person making it.

    Without this the four addressee signals miss every "i'll do X" said into a
    channel, which is exactly what "what do I need to do today" is asking for.
    """
    found = CandidateFilter().candidates([message(1, ALICE, "i'll push the fix tonight")])
    assert [c.signal for c in found] == [AddresseeSignal.FIRST_PERSON]


def test_ordinary_conversation_is_not_a_candidate() -> None:
    """The cost control. Most traffic must not reach the model."""
    found = CandidateFilter().candidates(
        [
            message(1, ALICE, "the nightly build finished at 3am"),
            message(2, BOB, "postgres 17 landed in the image"),
            message(3, ALICE, "nice"),
        ]
    )
    assert found == []


def test_bot_messages_are_never_candidates() -> None:
    """Our own answers quote the corpus; extracting from them feeds it itself."""
    found = CandidateFilter(bots=frozenset({BOT})).candidates(
        [message(1, BOT, "can you confirm you want me to do that?")]
    )
    assert found == []


def test_joins_and_system_notices_are_not_candidates() -> None:
    """Platform ceremony arrives with an empty body and asks nothing."""
    found = CandidateFilter().candidates([message(1, ALICE, "   ")])
    assert found == []


def test_deleted_messages_are_not_candidates() -> None:
    found = CandidateFilter().candidates(
        [message(1, ALICE, "can you look at this", deleted_at=T0)]
    )
    assert found == []


def test_context_is_bounded_and_ordered() -> None:
    history = [message(i, BOB, f"line {i}") for i in range(1, 9)]
    target = message(9, ALICE, "can you take this one")
    found = CandidateFilter(context_messages=3).candidates([*history, target])
    assert len(found) == 1
    assert [m.platform_message_id for m in found[0].context] == [6, 7, 8]


def test_a_reply_parent_outside_the_window_is_still_attached() -> None:
    parent = message(1, BOB, "the cluster has no terraform")
    reply = message(2, ALICE, "can you write it", reply_to_id=1)
    found = CandidateFilter().candidates([reply], parents={1: parent})
    assert found[0].reply_parent == parent
