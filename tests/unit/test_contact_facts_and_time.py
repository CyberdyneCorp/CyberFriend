"""Contact facts, a saved wallet as the asker's own words, and the clock.

The wallet part is the one that touches an invariant. An outbound query must be
rooted in the asker's words, and a saved address is not in the question they
just typed. The rule is not relaxed: the root set widens from "the words of
this question" to "the words this person wrote about themselves", and the check
stays containment against the exact values the store holds -- so a model's
invention, or an address in a channel message, still cannot pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chatmemory.app.egress import (
    ProvenancedQuery,
    QueryOrigin,
    RefusalReason,
    refusal_for,
)
from chatmemory.app.facts import DIRECT_ONLY_KINDS, visible_facts
from chatmemory.app.reasoning.ports import PromptContext
from chatmemory.app.reasoning.stages import clock_notice, with_context
from chatmemory.ports.facts import FactKind, InvalidFact, PersonalFact, PersonalFacts, StoredFact
from chatmemory.ports.memory import ConversationLocation

WALLET = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
OTHER_WALLET = "0x00000000219ab540356cbb839cbe05303d7705fa"
BTC = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


# --- the new kinds ------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "value", "stored"),
    [
        (FactKind.PHONE, " +55 11 99999-1234 ", "+55 11 99999-1234"),
        (FactKind.PHONE, "(11) 99999 1234", "(11) 99999 1234"),
        # EIP-55 mixes case as a checksum; lowercase cannot be a wrong one.
        (FactKind.ETH_WALLET, "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", WALLET),
        (FactKind.BTC_WALLET, BTC, BTC),
        (FactKind.BTC_WALLET, "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
         "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"),
    ],
)
def test_a_well_formed_value_is_stored_normalised(
    kind: FactKind, value: str, stored: str
) -> None:
    assert PersonalFact(kind, value).value == stored


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (FactKind.PHONE, "my mobile"),
        (FactKind.PHONE, "123"),
        (FactKind.PHONE, "1" * 20),
        (FactKind.ETH_WALLET, "0xnope"),
        (FactKind.ETH_WALLET, WALLET + "ff"),
        (FactKind.ETH_WALLET, BTC),
        (FactKind.BTC_WALLET, "not-a-wallet"),
        (FactKind.BTC_WALLET, WALLET),
    ],
)
def test_a_malformed_value_is_refused(kind: FactKind, value: str) -> None:
    """A wallet in the wrong field matters: an Ethereum lookup of a Bitcoin
    address returns a confident zero."""
    with pytest.raises(InvalidFact):
        PersonalFact(kind, value)


def test_contact_details_are_never_shown_outside_a_direct_message() -> None:
    """A wallet is public on its chain; what is private is that it is theirs,
    and a channel reply naming it makes that link for everyone present."""
    facts = PersonalFacts(
        tuple(
            StoredFact(PersonalFact(kind, value), datetime.now(UTC))
            for kind, value in (
                (FactKind.PREFERRED_NAME, "Leo"),
                (FactKind.EMAIL, "leo@example.com"),
                (FactKind.PHONE, "+55 11 99999 1234"),
                (FactKind.ETH_WALLET, WALLET),
                (FactKind.BTC_WALLET, BTC),
            )
        )
    )
    in_channel = visible_facts(facts, ConversationLocation("discord", 100, direct=False))
    shown = {s.fact.kind for s in in_channel.facts}

    assert shown == {FactKind.PREFERRED_NAME}
    for private in (FactKind.EMAIL, FactKind.PHONE, FactKind.ETH_WALLET, FactKind.BTC_WALLET):
        assert private in DIRECT_ONLY_KINDS

    in_dm = visible_facts(facts, ConversationLocation("discord", 0, direct=True))
    assert len(in_dm.facts) == 5


# --- a saved value as the asker's own words -----------------------------


def test_a_saved_value_is_admitted_for_its_owner() -> None:
    query = ProvenancedQuery(
        text=WALLET,
        origin=QueryOrigin.ASKER_FACT,
        question="what is my balance",
        asker_facts=frozenset({WALLET}),
    )
    assert refusal_for(query) is None


def test_without_the_saved_value_the_same_query_is_refused() -> None:
    """The label is evidence; the containment is the check."""
    query = ProvenancedQuery(
        text=WALLET, origin=QueryOrigin.ASKER_FACT, question="what is my balance"
    )
    assert refusal_for(query) is RefusalReason.NOT_ROOTED_IN_QUESTION


def test_one_persons_saved_value_does_not_admit_anothers_address() -> None:
    query = ProvenancedQuery(
        text=OTHER_WALLET,
        origin=QueryOrigin.ASKER_FACT,
        question="what is my balance",
        asker_facts=frozenset({WALLET}),
    )
    assert refusal_for(query) is RefusalReason.NOT_ROOTED_IN_QUESTION


def test_retrieved_content_is_refused_even_when_it_matches_a_saved_value() -> None:
    """The content gate runs before rooting, so an address in a message cannot
    trigger a lookup by coinciding with something the person saved."""
    query = ProvenancedQuery(
        text=WALLET,
        origin=QueryOrigin.RETRIEVED_CONTENT,
        question="what is my balance",
        asker_facts=frozenset({WALLET}),
    )
    assert refusal_for(query) is RefusalReason.CONTENT_DERIVED


def test_saved_values_default_to_empty() -> None:
    """A field that is empty by default cannot widen anything by accident."""
    assert ProvenancedQuery("x", QueryOrigin.ASKER, "x").asker_facts == frozenset()


def test_a_saved_value_is_trusted_no_further_than_the_asker() -> None:
    """Derivation takes the maximum taint, so a fact-derived query rewritten by
    a model does not stay fact-derived."""
    from chatmemory.app.egress import _TAINT

    assert _TAINT[QueryOrigin.ASKER_FACT] == _TAINT[QueryOrigin.ASKER]


# --- the clock ----------------------------------------------------------


def test_the_prompt_carries_the_current_time_with_its_zone() -> None:
    notice = clock_notice(datetime(2026, 9, 18, 19, 45, tzinfo=UTC))
    assert "18 September 2026" in notice
    assert "19:45" in notice
    assert "UTC" in notice, "a time without a zone reads as local to whoever looks"


def test_the_clock_reaches_the_prompt_even_with_no_other_context() -> None:
    """The early return for "no blocks" used to skip the notices entirely."""
    system, _ = with_context("answer the question", "q", PromptContext())
    assert "current date and time" in system


def test_the_clock_reaches_the_prompt_alongside_other_context() -> None:
    system, user = with_context(
        "answer", "q", PromptContext(asker="ASKER", memory="MEMORY")
    )
    assert "current date and time" in system
    assert "ASKER" in user and "MEMORY" in user


def test_the_clock_says_it_is_not_evidence() -> None:
    """Knowing the time must not turn an abstention into an answer."""
    notice = clock_notice()
    assert "not evidence" in notice
    assert "never a citation" in notice


# --- asking what the date is --------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        # The verbatim message from Discord. It reached retrieval, found
        # nothing, and was answered "I couldn't find anything about that in
        # the messages you can see" -- the grounding rule working correctly on
        # a question that should never have reached it.
        "what's today's date ?",
        "what is the date?",
        "what time is it",
        "what day is it today",
        "que dia é hoje?",
        "qual a data de hoje",
        "que horas são?",
    ],
)
def test_asking_the_date_is_its_own_route(question: str) -> None:
    from chatmemory.app.routing import time_question

    assert time_question(question)


@pytest.mark.parametrize(
    "question",
    [
        "what was decided today",
        "what time did the deploy finish",
        "what did people ask me today",
        "summarise what happened today",
        "o que foi decidido hoje",
    ],
)
def test_a_question_about_the_corpus_that_mentions_time_is_not_diverted(
    question: str,
) -> None:
    """Anchored at both ends: only a question whose whole content is the clock
    belongs on this route."""
    from chatmemory.app.routing import time_question

    assert not time_question(question)


def test_the_time_answer_states_its_zone_and_its_language() -> None:
    from chatmemory.app.language import Language
    from chatmemory.app.reasoning.service import current_time_answer

    moment = datetime(2026, 9, 18, 20, 39, tzinfo=UTC)
    english = current_time_answer(Language.ENGLISH, moment)
    portuguese = current_time_answer(Language.PORTUGUESE, moment)

    for answer in (english, portuguese):
        assert "18 September 2026" in answer
        assert "20:39" in answer
        assert "UTC" in answer, "a time without a zone reads as local"
    assert english.startswith("It is")
    assert portuguese.startswith("Agora")
