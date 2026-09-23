"""Several facts in one message, and a full name.

Written from production. This message saved nothing:

    Oi meu nome e Leonardo Araujo dos Santos, pode me chamar de Leo, meu email
    e leonardoaraujo.santos@gmail.com e moro no Brasi, Rio de Janeiro, meu
    telefone e +55 21 980703795

Each fact was only recognised alone, so it became an ordinary question: it was
answered from channel messages ("Não posso confirmar esses dados com as
evidências mostradas"), and the whole text -- email and phone included -- was
kept as a remembered conversation turn. It would also have been archived had
it been sent in an indexed channel. And "oque voce sabe sobre mim ?" was not
recognised as asking what the assistant knows.
"""

from __future__ import annotations

import json

import pytest

from chatmemory.app.ask import fact_set_reply, facts_set_reply
from chatmemory.app.asker import AskerFacts, _payload
from chatmemory.app.facts import FactOutcome, FactResult
from chatmemory.app.routing import (
    WHERE_YOU_LIVE,
    FactAction,
    fact_intent,
    states_own_contact,
)
from chatmemory.ports.facts import FactKind, PersonalFact
from tests.unit.test_conversation_memory import FakeMemoryStore, conversations
from tests.unit.test_facts_behaviour import LEO, build, in_channel, in_dm, reply

INTRODUCTION = (
    "Oi meu nome e Leonardo Araujo dos Santos, pode me chamar de Leo, meu email "
    "e leonardoaraujo.santos@gmail.com e moro no Brasi, Rio de Janeiro, meu "
    "telefone e +55 21 980703795"
)
EMAIL = "leonardoaraujo.santos@gmail.com"
PHONE = "+55 21 980703795"


def test_the_production_introduction_is_every_fact_it_states() -> None:
    intent = fact_intent(INTRODUCTION)
    assert intent is not None
    assert intent.action is FactAction.SET_MANY
    assert intent.sets == (
        (FactKind.FULL_NAME, "Leonardo Araujo dos Santos"),
        (FactKind.PREFERRED_NAME, "Leo"),
        (FactKind.EMAIL, EMAIL),
        (FactKind.PHONE, PHONE),
    )
    assert intent.not_kept == (WHERE_YOU_LIVE,)


async def test_an_introduction_is_saved_and_never_answered_or_remembered() -> None:
    memory = FakeMemoryStore()
    service, store, answers = build(memory=conversations(memory))

    text = await reply(service, in_dm(INTRODUCTION))

    assert answers.seen == [], "the introduction was answered from the corpus"
    assert memory.turns == [], "email and phone were kept as a conversation turn"
    assert store.rows[(LEO, FactKind.FULL_NAME)] == "Leonardo Araujo dos Santos"
    assert store.rows[(LEO, FactKind.PREFERRED_NAME)] == "Leo"
    assert store.rows[(LEO, FactKind.EMAIL)] == EMAIL
    assert store.rows[(LEO, FactKind.PHONE)] == PHONE
    # In a DM every value is repeated back, so a mis-split is visible at once.
    for shown in ("Leonardo Araujo dos Santos", "Leo", EMAIL, PHONE):
        assert shown in text
    # Answered in the language it was written in.
    assert text.startswith("Aqui está o que salvei:")
    assert "Onde você mora: não guardo isso" in text


async def test_in_a_channel_contact_details_are_confirmed_without_their_values() -> None:
    service, store, _ = build()
    text = await reply(service, in_channel(INTRODUCTION))

    assert store.rows[(LEO, FactKind.EMAIL)] == EMAIL
    assert EMAIL not in text and "980703795" not in text
    assert "E-mail: salvo (mostrado só em mensagem direta)" in text
    assert "Leonardo Araujo dos Santos" in text  # a name is not contact data


@pytest.mark.parametrize(
    "text",
    [
        INTRODUCTION,
        "meu telefone e +55 21 980703795",
        "my phone number is +55 11 99999 1234",
        # Not an introduction, and still gives an email.
        "call me when the deploy is done and my email is leo@example.com",
    ],
)
def test_contact_details_stated_in_a_channel_are_withheld_from_the_corpus(text: str) -> None:
    assert states_own_contact(text)


@pytest.mark.parametrize(
    "text",
    ["call me Leo", "my name is Leonardo Araujo", "what's my wallet balance?"],
)
def test_messages_without_contact_details_are_indexed_as_usual(text: str) -> None:
    assert not states_own_contact(text)


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("my name is Leo", FactKind.PREFERRED_NAME, "Leo"),
        ("my name is Leonardo Araujo", FactKind.FULL_NAME, "Leonardo Araujo"),
        ("meu nome é Leonardo Araujo dos Santos", FactKind.FULL_NAME,
         "Leonardo Araujo dos Santos"),
        ("my full name is Leonardo", FactKind.FULL_NAME, "Leonardo"),
        ("meu nome completo é Leonardo Araujo", FactKind.FULL_NAME, "Leonardo Araujo"),
        ("my preferred name is Leo Araujo", FactKind.PREFERRED_NAME, "Leo Araujo"),
    ],
)
def test_a_multi_word_name_is_a_full_name(text: str, kind: FactKind, value: str) -> None:
    intent = fact_intent(text)
    assert intent is not None
    assert (intent.action, intent.kind, intent.value) == (FactAction.SET, kind, value)


@pytest.mark.parametrize(
    "text",
    [
        "call me Leo, my email is leo@example.com",
        "my name is Leo and my wallet is 0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0",
        "you can call me Leo and my phone is 555 1234",
    ],
)
def test_two_facts_in_english_are_both_recognised(text: str) -> None:
    intent = fact_intent(text)
    assert intent is not None and intent.action is FactAction.SET_MANY
    assert len(intent.sets) == 2


@pytest.mark.parametrize(
    "text",
    [
        "my team said the deploy is done and my manager agrees",
        "call me when the deploy is done",
        "what is my wallet balance?",
    ],
)
def test_ordinary_questions_with_my_are_not_introductions(text: str) -> None:
    intent = fact_intent(text)
    assert intent is None or intent.action is not FactAction.SET_MANY


@pytest.mark.parametrize(
    "text", ["oque voce sabe sobre mim ?", "oq vc sabe sobre mim?", "o que você sabe sobre mim?"]
)
def test_asking_what_it_knows_is_recognised_as_typed(text: str) -> None:
    intent = fact_intent(text)
    assert intent is not None and intent.action is FactAction.SHOW


async def test_what_it_knows_is_shown_not_searched() -> None:
    service, _, answers = build()
    await reply(service, in_dm(INTRODUCTION))

    text = await reply(service, in_dm("oque voce sabe sobre mim ?"))

    assert answers.seen == []
    assert "Leonardo Araujo dos Santos" in text and PHONE in text


def test_a_saved_phone_is_confirmed_as_a_phone_not_an_email() -> None:
    """PR #48 confirmed every direct-only fact as "your email address"."""
    for kind, value, label in (
        (FactKind.PHONE, PHONE, "phone number"),
        (FactKind.ETH_WALLET, "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0", "Ethereum wallet"),
    ):
        result = FactResult(FactOutcome.STORED, kind, PersonalFact(kind, value))
        for direct in (True, False):
            text = fact_set_reply(result, direct)
            assert label in text
            assert "email" not in text


def test_a_refused_part_of_an_introduction_is_named_without_its_value() -> None:
    results = [
        FactResult(FactOutcome.STORED, FactKind.PREFERRED_NAME,
                   PersonalFact(FactKind.PREFERRED_NAME, "Leo")),
        FactResult(FactOutcome.REJECTED, FactKind.EMAIL, rejection=None),
    ]
    text = facts_set_reply(results, (), direct=True)
    assert "✓ Preferred name: **Leo**" in text
    assert "✗ Email address: not saved, it isn't a well-formed email address" in text


def test_the_full_name_reaches_the_prompt_beside_the_preferred_name() -> None:
    payload = json.loads(
        _payload(None, AskerFacts(LEO, preferred_name="Leo", full_name="Leonardo Araujo"))
    )
    assert payload["preferred_name"] == "Leo"
    assert payload["full_name"] == "Leonardo Araujo"
