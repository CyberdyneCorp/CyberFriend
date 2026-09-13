"""Who an ask falls to.

This is the likeliest source of wrong entries, so every path is tested on its
own -- including the one that declines to answer. An ask recorded against the
wrong person is worse than one recorded against nobody: the first is a
confident false obligation, the second is visibly incomplete.
"""

from __future__ import annotations

from chatmemory.app.asks.model import AddresseeKind, AddresseeSignal, AskKind
from chatmemory.app.asks.resolution import (
    MENTIONED_GROUP,
    StaticDirectory,
    normalise_name,
    resolve_addressee,
)
from chatmemory.domain.identity import PersonRef
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    CARA,
    candidate,
    extracted,
    message,
)

DIRECTORY = StaticDirectory({"alice": ALICE, "bob": BOB, "cara": CARA})


def test_an_explicit_mention_resolves_to_that_person() -> None:
    found = resolve_addressee(
        candidate(message(1, ALICE, "@bob can you review this", mentions=frozenset({BOB}))),
        extracted(),
        DIRECTORY,
    )
    assert found.person == BOB


def test_a_reply_resolves_to_the_parent_author() -> None:
    parent = message(1, BOB, "migration 0002 is merged")
    reply = message(2, ALICE, "could you re-run it on staging?", reply_to_id=1)
    found = resolve_addressee(
        candidate(reply, AddresseeSignal.REPLY, reply_parent=parent), extracted(), DIRECTORY
    )
    assert found.person == BOB


def test_a_name_in_the_text_beats_the_reply_parent() -> None:
    """"Unless the ask names someone else" -- the reply chain is the fallback."""
    parent = message(1, BOB, "the deck needs finishing")
    reply = message(2, ALICE, "cara can you send it over?", reply_to_id=1)
    found = resolve_addressee(
        candidate(reply, AddresseeSignal.REPLY, reply_parent=parent),
        extracted(addressee_hint="cara"),
        DIRECTORY,
    )
    assert found.person == CARA


def test_a_name_that_resolves_to_nobody_is_unattributed_not_the_reply_parent() -> None:
    """The ask names someone. Attributing it to anyone else invents an obligation."""
    parent = message(1, BOB, "the cluster still has no terraform")
    reply = message(2, ALICE, "can nadia take a look at it?", reply_to_id=1)
    found = resolve_addressee(
        candidate(reply, AddresseeSignal.REPLY, reply_parent=parent),
        extracted(addressee_hint="nadia"),
        DIRECTORY,
    )
    assert found.kind is AddresseeKind.UNATTRIBUTED


def test_an_ambiguous_name_is_unattributed() -> None:
    """Two people called alex make every ask to either of them a coin flip."""
    alex_one = PersonRef("discord", 41)
    alex_two = PersonRef("discord", 42)
    directory = StaticDirectory([("alex", alex_one), ("alex", alex_two)])
    found = resolve_addressee(
        candidate(message(1, ALICE, "alex can you take this?")),
        extracted(addressee_hint="alex"),
        directory,
    )
    assert found.kind is AddresseeKind.UNATTRIBUTED


def test_a_group_ask_is_recorded_as_a_group() -> None:
    found = resolve_addressee(
        candidate(message(1, ALICE, "can someone on the platform team look at this?")),
        extracted(addressee_hint="the platform team", addressee_is_group=True),
        DIRECTORY,
    )
    assert found.kind is AddresseeKind.GROUP
    assert found.person is None


def test_a_group_word_in_the_text_is_a_group_even_without_a_hint() -> None:
    found = resolve_addressee(
        candidate(message(1, ALICE, "can anyone pick up the failing nightly?")),
        extracted(),
        DIRECTORY,
    )
    assert found.kind is AddresseeKind.GROUP


def test_several_mentions_are_a_group_not_an_arbitrary_member() -> None:
    found = resolve_addressee(
        candidate(
            message(1, ALICE, "can you two sort the rota?", mentions=frozenset({BOB, CARA}))
        ),
        extracted(),
        DIRECTORY,
    )
    assert found.kind is AddresseeKind.GROUP
    assert found.group == MENTIONED_GROUP


def test_several_mentions_resolve_when_the_model_disambiguates() -> None:
    found = resolve_addressee(
        candidate(
            message(1, ALICE, "@bob @cara bob can you take it?", mentions=frozenset({BOB, CARA}))
        ),
        extracted(addressee_hint="bob"),
        DIRECTORY,
    )
    assert found.person == BOB


def test_nothing_determinable_is_unattributed() -> None:
    found = resolve_addressee(
        candidate(message(1, ALICE, "this really needs picking up at some point")),
        extracted(),
        DIRECTORY,
    )
    assert found.kind is AddresseeKind.UNATTRIBUTED
    assert found.person is None


def test_a_commitment_falls_to_the_person_who_made_it() -> None:
    found = resolve_addressee(
        candidate(message(1, ALICE, "i'll push the fix tonight", mentions=frozenset({BOB}))),
        extracted(kind=AskKind.COMMITMENT, addressee_hint="bob"),
        DIRECTORY,
    )
    assert found.person == ALICE


def test_a_pronoun_hint_falls_through_to_the_reply_chain() -> None:
    """Models restate the addressee as "you". That carries no new information."""
    parent = message(1, BOB, "the sync is stuck")
    reply = message(2, ALICE, "can you restart it?", reply_to_id=1)
    found = resolve_addressee(
        candidate(reply, AddresseeSignal.REPLY, reply_parent=parent),
        extracted(addressee_hint="you"),
        DIRECTORY,
    )
    assert found.person == BOB


def test_a_placeholder_hint_is_not_treated_as_a_name() -> None:
    found = resolve_addressee(
        candidate(message(1, ALICE, "someone needs to own this")),
        extracted(addressee_hint="unknown"),
        DIRECTORY,
    )
    assert found.kind is AddresseeKind.GROUP


def test_self_mention_does_not_make_the_author_the_addressee() -> None:
    found = resolve_addressee(
        candidate(
            message(1, ALICE, "as i said, can this get picked up", mentions=frozenset({ALICE}))
        ),
        extracted(),
        DIRECTORY,
    )
    assert found.kind is AddresseeKind.UNATTRIBUTED


def test_names_normalise_across_punctuation_and_case() -> None:
    assert normalise_name("@Leo,") == "leo"
    assert normalise_name("  O'Brien ") == "o'brien"
