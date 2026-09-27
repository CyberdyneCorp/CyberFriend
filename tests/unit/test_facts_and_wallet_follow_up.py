"""Four production misses in one Portuguese DM, pinned by the sentences sent.

    Guarde na sua memoria que eu quero os meus valores em reais brasileiro
    Oque vide sabe sobre mim ?
    Você sabe qual é a moeda do meu país ?
    verifique as pools de liquidez nesta wallet 0xB26B…45e0
    ... -> "Qual carteira?"
    A carteira que eu acabei de passar

The first three were not recognised as facts and went to the corpus. The last
two asked for an address the asker had typed two turns before. The end-to-end
scenarios are in tests/e2e/test_facts_and_wallet_follow_up.py.
"""

from __future__ import annotations

import pytest

from chatmemory.app.alert_intent import alert_intent
from chatmemory.app.fact_replies import facts_shown_reply
from chatmemory.app.language import Language
from chatmemory.app.routing import (
    FactAction,
    asker_typed_address,
    fact_intent,
    wallet_reference,
)
from chatmemory.app.routing_crypto import CryptoRoute, crypto_route
from chatmemory.domain.currency import currency_code
from chatmemory.ports.facts import FactKind, PersonalFacts

WALLET = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"
OTHER = "0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5"
POOLS = f"verifique as pools de liquidez nesta wallet {WALLET}"


# --- 1. a currency kept "in memory" ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Guarde na sua memoria que eu quero os meus valores em reais brasileiro",
         "reais brasileiro"),
        ("guarde na sua memória que eu quero ver os valores em reais", "reais"),
        ("lembre que eu quero os meus preços em euros", "euros"),
        ("anote que prefiro ver em real brasileiro", "real brasileiro"),
        ("remember that I want to see my prices in euros", "euros"),
        ("eu quero os valores em reais brasileiros", "reais brasileiros"),
    ],
)
def test_a_currency_behind_a_remember_wrapper_is_saved(text: str, value: str) -> None:
    intent = fact_intent(text)
    assert intent is not None and intent.action is FactAction.SET
    assert (intent.kind, intent.value) == (FactKind.PREFERRED_CURRENCY, value)
    assert currency_code(value) is not None


@pytest.mark.parametrize(
    "said", ["reais brasileiro", "real brasileiros", "reais brasileiros", "real brasileiro"]
)
def test_every_number_and_gender_of_the_real_is_brl(said: str) -> None:
    assert currency_code(said) == "BRL"


@pytest.mark.parametrize(
    "text",
    [
        "quanto é 100 reais em dólar?",
        "quero trocar 100 reais em dólar",
        "guarde 100 reais em dólar",
        "quero os meus valores em reais do mês passado",
        "lembre que eu quero comprar 100 reais em bitcoin",
    ],
)
def test_money_talk_behind_the_same_words_is_not_a_preference(text: str) -> None:
    intent = fact_intent(text)
    assert intent is None or intent.kind is not FactKind.PREFERRED_CURRENCY


# --- 2. "what do you know about me", typed as people type ----------------------


@pytest.mark.parametrize(
    "text",
    [
        "Oque vide sabe sobre mim ?",
        "o que vc sabe sobre mim?",
        "oq tu sabe sobre mim",
        "o que voce sabe de mim?",
        "what do u know about me?",
        "what do you know about me?",
    ],
)
def test_asking_what_the_assistant_knows_shows_the_facts(text: str) -> None:
    assert fact_intent(text) == fact_intent("o que você sabe sobre mim?")
    intent = fact_intent(text)
    assert intent is not None and (intent.action, intent.kind) == (FactAction.SHOW, None)


@pytest.mark.parametrize(
    "text",
    [
        "o que o João sabe sobre mim?",
        "o que João sabe sobre mim?",
        "o que ele sabe sobre mim?",
        "o que a Ana sabe sobre mim?",
        "what does John know about me?",
        "what do they know about me?",
    ],
)
def test_somebody_else_knowing_about_me_is_not_my_facts(text: str) -> None:
    intent = fact_intent(text)
    assert intent is None or intent.action is not FactAction.SHOW


# --- 3. the currency, asked for without "minha" ---------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Você sabe qual é a moeda do meu país ?",
        "qual a moeda do meu país?",
        "qual é a minha moeda de base?",
        "qual a minha moeda?",
        "what's my currency?",
        "what is the currency of my country?",
        "which currency do I use?",
    ],
)
def test_the_currency_asked_for_any_way_is_shown(text: str) -> None:
    intent = fact_intent(text)
    assert intent is not None
    assert (intent.action, intent.kind) == (FactAction.SHOW, FactKind.PREFERRED_CURRENCY)


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (Language.PORTUGUESE, "Diga `minha moeda é o real`"),
        (Language.ENGLISH, "Say `my currency is the euro`"),
    ],
)
def test_no_currency_saved_says_how_to_save_one(language: Language, expected: str) -> None:
    for direct in (True, False):
        text = facts_shown_reply(
            PersonalFacts(()), direct, language, FactKind.PREFERRED_CURRENCY
        )
        assert expected in text


# --- 4. the wallet the asker just gave ------------------------------------------


def test_the_latest_address_the_asker_typed_is_found_in_any_earlier_question() -> None:
    previous = (f"saldo de {OTHER}?", POOLS, "e o valor em reais?")
    assert asker_typed_address(previous) == WALLET.lower()
    assert asker_typed_address(("oi", "tudo bem?")) is None


@pytest.mark.parametrize(
    "text",
    [
        "A carteira que eu acabei de passar",
        "essa carteira",
        "nessa carteira?",
        "a carteira que eu te mandei antes",
        "use o mesmo endereço",
        "the wallet I just gave you",
        "that address",
    ],
)
def test_pointing_back_at_a_wallet_is_recognised(text: str) -> None:
    assert wallet_reference(text)


@pytest.mark.parametrize(
    "text", ["qual o saldo dessa carteira", "carteira do João", "the wallet of Ana"]
)
def test_a_question_about_a_wallet_is_not_only_pointing_back(text: str) -> None:
    assert not wallet_reference(text)


def test_the_bare_follow_up_asks_the_last_chain_question_again_with_that_address() -> None:
    previous = (POOLS, "e quanto vale isso nas minhas pools em reais?")

    query = crypto_route("A carteira que eu acabei de passar", previous)

    assert query is not None
    assert query.route is CryptoRoute.DEFI_LIQUIDITY
    assert query.addresses == (WALLET.lower(),)
    assert query.asked == previous[-1]
    assert query.carried and not query.mine


def test_the_bare_follow_up_with_no_address_typed_asks_for_one() -> None:
    query = crypto_route("essa carteira", ("quais as minhas pools de liquidez?",))
    assert query is not None
    assert query.addresses == () and not query.mine, "never the saved wallet"


def test_the_bare_follow_up_after_no_chain_question_is_not_a_chain_question() -> None:
    assert crypto_route("essa carteira", (f"o que disseram sobre {WALLET}?",)) is None
    assert crypto_route("essa carteira", ()) is None


def test_an_alert_with_no_address_carries_the_one_the_asker_typed_earlier() -> None:
    # Further back than a chain follow-up reaches: only the typed-earlier rule.
    earlier = (POOLS, "obrigado", "e o bitcoin?", "que dia é hoje?")
    intent = alert_intent("me avise quando minha posição sair do range", earlier)
    assert intent is not None
    assert intent.address is None
    assert intent.earlier_address == WALLET.lower()
