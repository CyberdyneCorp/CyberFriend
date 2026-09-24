"""Richer personal facts: address, birth date, several wallets, robust introductions.

Written from production. This DM saved only the full name, and refused the
phone number as "too long":

    Oi Me chamo Leonardo Araujo dos Santos pode me Chamar de Leo, tenho 45
    anos nasci em 21/06/1981 meu telefone e +5521980703795 morro no Rio de
    Janeiro Brasil, Vargem Grande minha walet e 0xB26B...75e0 meu email
    leonardoaraujo.santos@gmail.com

A clause ran to the next recognised starter, and "tenho", "nasci" and "morro"
were not starters: the preferred name was "Leo, tenho 45 anos nasci em
21/06/1981" (implausible, so dropped), and the phone was the number plus the
address. "walet" was not a wallet, and "meu email X" without "é" was not an
email. Where somebody lives and when they were born were not kept at all.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from chatmemory.app.ask import fact_forgotten_reply
from chatmemory.app.asker import AskerFacts, _payload
from chatmemory.app.fact_replies import possessive, shown_value
from chatmemory.app.facts import FactOutcome, PersonalFactsService
from chatmemory.app.language import Language
from chatmemory.app.reasoning.service import (
    WHICH_SAVED_WALLET,
    _lookup_addresses,
)
from chatmemory.app.routing import (
    AGE,
    WHERE_YOURE_FROM,
    FactAction,
    fact_intent,
    states_own_contact,
)
from chatmemory.app.routing_crypto import crypto_route
from chatmemory.domain.chain import named_by_suffix, suffix_list
from chatmemory.ports.facts import (
    MAX_HOME_ADDRESS_CHARS,
    MAX_WALLETS_PER_KIND,
    FactKind,
    FactRejection,
    InvalidFact,
    normalise_fact,
)
from tests.unit.test_external_routing import WEB
from tests.unit.test_facts import FakeFactStore, viewer
from tests.unit.test_facts_behaviour import LEO, build, in_channel, in_dm, reply
from tests.unit.test_loop_invocation import ScriptedSurface
from tests.unit.test_reasoning_fixed import FakeRetrieval, question
from tests.unit.test_wallet_routing import (
    SOMEBODY_ELSES_PROJECT,
    WANTS_BALANCES,
    ParaphrasingSynthesizer,
    service,
)
from tests.unit.test_wallet_routing import WALLET as WALLET_TOOL

PRODUCTION = (
    "Oi Me chamo Leonardo Araujo dos Santos pode me Chamar de Leo, tenho 45 anos "
    "nasci em 21/06/1981 meu telefone e +5521980703795 morro no Rio de Janeiro "
    "Brasil, Vargem Grande minha walet e 0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0 "
    "meu email leonardoaraujo.santos@gmail.com"
)
WALLET_TYPED = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"
WALLET = WALLET_TYPED.lower()
OTHER = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
EMAIL = "leonardoaraujo.santos@gmail.com"
ADDRESS = "Rio de Janeiro Brasil, Vargem Grande"


def sets(text: str) -> dict[FactKind, str]:
    intent = fact_intent(text)
    assert intent is not None, text
    if intent.action is FactAction.SET:
        assert intent.kind is not None and intent.value is not None
        return {intent.kind: intent.value}
    assert intent.action is FactAction.SET_MANY, intent
    return dict(intent.sets)


# --- the production message -------------------------------------------------


def test_the_production_introduction_is_every_fact_it_states() -> None:
    intent = fact_intent(PRODUCTION)
    assert intent is not None and intent.action is FactAction.SET_MANY
    assert intent.sets == (
        (FactKind.FULL_NAME, "Leonardo Araujo dos Santos"),
        (FactKind.PREFERRED_NAME, "Leo"),
        (FactKind.BIRTH_DATE, "21/06/1981"),
        (FactKind.PHONE, "+5521980703795"),
        (FactKind.HOME_ADDRESS, ADDRESS),
        (FactKind.ETH_WALLET, WALLET_TYPED),
        (FactKind.EMAIL, EMAIL),
    )
    assert intent.not_kept == (AGE,)


async def test_the_production_introduction_is_saved_and_confirmed_in_portuguese() -> None:
    service, store, answers = build()

    text = await reply(service, in_dm(PRODUCTION))

    assert answers.seen == []
    assert store.rows[(LEO, FactKind.FULL_NAME)] == "Leonardo Araujo dos Santos"
    assert store.rows[(LEO, FactKind.PREFERRED_NAME)] == "Leo"
    assert store.rows[(LEO, FactKind.PHONE)] == "+5521980703795"
    assert store.rows[(LEO, FactKind.HOME_ADDRESS)] == ADDRESS
    assert store.rows[(LEO, FactKind.BIRTH_DATE)] == "1981-06-21"
    assert store.rows[(LEO, FactKind.EMAIL)] == EMAIL
    assert store.wallets[(LEO, FactKind.ETH_WALLET)] == [WALLET]
    assert text.splitlines() == [
        "Aqui está o que salvei:",
        "✓ Nome completo: **Leonardo Araujo dos Santos**",
        "✓ Nome preferido: **Leo**",
        "✓ Data de nascimento: **21 de junho de 1981**",
        "✓ Telefone: **+5521980703795**",
        f"✓ Endereço: **{ADDRESS}**",
        # "Ethereum" keeps its capital: `str.capitalize` once lowered it.
        f"✓ Carteira Ethereum: **{WALLET}**",
        f"✓ E-mail: `{EMAIL}`",
        "✗ Idade: não guardo, ela vem da sua data de nascimento",
    ]


async def test_in_a_channel_nothing_private_from_the_introduction_is_repeated() -> None:
    service, store, _ = build()

    text = await reply(service, in_channel(PRODUCTION))

    assert store.rows[(LEO, FactKind.HOME_ADDRESS)] == ADDRESS
    for private in ("980703795", "Vargem", "1981", WALLET, EMAIL):
        assert private not in text
    assert "✓ Endereço: salvo (mostrado só em mensagem direta)" in text
    assert "✓ Data de nascimento: salvo (mostrado só em mensagem direta)" in text
    assert "Leonardo Araujo dos Santos" in text


def test_the_production_introduction_is_withheld_from_the_corpus() -> None:
    assert states_own_contact(PRODUCTION)


@pytest.mark.parametrize(
    "text", ["moro em Vargem Grande, Rio de Janeiro", "nasci em 21/06/1981", "i live in Lisbon"]
)
def test_an_address_or_birth_date_alone_is_withheld_from_the_corpus(text: str) -> None:
    assert states_own_contact(text)


# --- value cutting ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("pode me chamar de Leo, sou dev", FactKind.PREFERRED_NAME, "Leo"),
        ("call me Leo and I'm a dev", FactKind.PREFERRED_NAME, "Leo"),
        ("me chamo Leo e sou dev", FactKind.PREFERRED_NAME, "Leo"),
        ("meu nome é Leonardo Araujo e sou dev", FactKind.FULL_NAME, "Leonardo Araujo"),
        # " e " before a capitalised word is part of a Brazilian surname.
        ("meu nome é Maria Araujo e Silva", FactKind.FULL_NAME, "Maria Araujo e Silva"),
        ("my name is Leonardo A. Santos", FactKind.FULL_NAME, "Leonardo A. Santos"),
        ("meu telefone é +55 (21) 98070-3795 me liga", FactKind.PHONE, "+55 (21) 98070-3795"),
        ("nasci em 21/06/1981 no Rio", FactKind.BIRTH_DATE, "21/06/1981"),
    ],
)
def test_a_value_stops_where_the_person_moved_on(text: str, kind: FactKind, value: str) -> None:
    assert sets(text) == {kind: value}


def test_an_introduction_clause_does_not_run_the_name_into_the_age() -> None:
    assert sets("me chamo Leo, tenho 45 anos, meu email é leo@example.com") == {
        FactKind.PREFERRED_NAME: "Leo",
        FactKind.EMAIL: "leo@example.com",
    }


# --- typos and missing verbs -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"minha walet e {WALLET_TYPED}",
        f"minha wallet é {WALLET_TYPED}",
        f"minha carteira {WALLET_TYPED}",
        f"my walet is {WALLET_TYPED}",
        f"my wallet {WALLET_TYPED}",
        f"add my wallet {WALLET_TYPED}",
        f"adicione a carteira {WALLET_TYPED}",
    ],
)
def test_every_way_of_giving_a_wallet(text: str) -> None:
    assert sets(text) == {FactKind.ETH_WALLET: WALLET_TYPED}


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("meu email leo@example.com", FactKind.EMAIL, "leo@example.com"),
        ("my email: leo@example.com", FactKind.EMAIL, "leo@example.com"),
        ("meu telefone +5521980703795", FactKind.PHONE, "+5521980703795"),
        ("my phone 021 98070 3795", FactKind.PHONE, "021 98070 3795"),
    ],
)
def test_a_value_of_the_right_shape_needs_no_verb(
    text: str, kind: FactKind, value: str
) -> None:
    assert sets(text) == {kind: value}


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("morro no Rio de Janeiro", "Rio de Janeiro"),
        ("moro em Vargem Grande", "Vargem Grande"),
        ("eu moro na Rua das Flores, 12", "Rua das Flores, 12"),
        ("meu endereço é Rua das Flores 12, Rio", "Rua das Flores 12, Rio"),
        ("I live in Lisbon, Portugal", "Lisbon, Portugal"),
        ("my home address is 10 Downing St, London", "10 Downing St, London"),
    ],
)
def test_a_home_address_in_either_language(text: str, value: str) -> None:
    assert sets(text) == {FactKind.HOME_ADDRESS: value}


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("nasci em 21/06/1981", "21/06/1981"),
        ("nasci em 21 de junho de 1981", "21 de junho de 1981"),
        ("minha data de nascimento é 1981-06-21", "1981-06-21"),
        ("I was born on June 21 1981", "June 21 1981"),
        ("born on 21 June 1981", "21 June 1981"),
        ("my birthday is 21-06-1981", "21-06-1981"),
    ],
)
def test_a_birth_date_in_either_language(text: str, value: str) -> None:
    assert sets(text) == {FactKind.BIRTH_DATE: value}


@pytest.mark.parametrize(
    "text",
    [
        # "morro" is also "I die" and "hill": without "no/na/em" it is neither.
        "morro de medo de aranha",
        "my email bounced",
        "what's my wallet balance?",
        "tenho 45 anos",
        "why does everyone say I live in the office?",
    ],
)
def test_what_is_not_a_fact_statement(text: str) -> None:
    intent = fact_intent(text)
    assert intent is None or intent.action not in (FactAction.SET, FactAction.SET_MANY), intent


def test_where_somebody_is_from_is_not_kept_as_an_address() -> None:
    intent = fact_intent("me chamo Leo e sou de Recife")
    assert intent is not None and intent.action is FactAction.SET_MANY
    assert intent.not_kept == (WHERE_YOURE_FROM,)


# --- the new kinds' validation -------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["21/06/1981", "21-06-1981", "21.06.1981", "1981-06-21", "21 de junho de 1981",
     "June 21 1981", "June 21st, 1981", "21 June 1981", "06/21/1981"],
)
def test_birth_dates_are_stored_iso(value: str) -> None:
    assert normalise_fact(FactKind.BIRTH_DATE, value) == "1981-06-21"


@pytest.mark.parametrize(
    "value",
    [
        "31/02/1981",
        "21/13/1981",
        "1850-01-01",
        (date.today() + timedelta(days=1)).isoformat(),
        "21 de junho",
        "soon",
        "",
    ],
)
def test_impossible_future_and_partial_birth_dates_are_refused(value: str) -> None:
    with pytest.raises(InvalidFact):
        normalise_fact(FactKind.BIRTH_DATE, value)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Rua **das** Flores", FactRejection.DISALLOWED_CHARACTERS),
        ("<@123> street", FactRejection.DISALLOWED_CHARACTERS),
        ("Rua `x`", FactRejection.DISALLOWED_CHARACTERS),
        ("a" * (MAX_HOME_ADDRESS_CHARS + 1), FactRejection.TOO_LONG),
        (" , ", FactRejection.EMPTY),
    ],
)
def test_a_home_address_carries_no_markup(value: str, reason: FactRejection) -> None:
    with pytest.raises(InvalidFact) as refused:
        normalise_fact(FactKind.HOME_ADDRESS, value)
    assert refused.value.reason is reason


def test_a_home_address_keeps_what_addresses_use() -> None:
    assert (
        normalise_fact(FactKind.HOME_ADDRESS, "Av. das Américas, nº 3/201 (bloco 2)")
        == "Av. das Américas, nº 3/201 (bloco 2)"
    )


# --- several wallets ----------------------------------------------------------


async def test_a_second_wallet_is_added_and_both_are_shown() -> None:
    service, store, _ = build()
    await reply(service, in_dm(f"minha carteira é {WALLET_TYPED}"))
    await reply(service, in_dm(f"my wallet is {OTHER}"))

    assert store.wallets[(LEO, FactKind.ETH_WALLET)] == [WALLET, OTHER]
    shown = await reply(service, in_dm("quais são minhas carteiras?"))
    assert WALLET in shown and OTHER in shown


async def test_the_wallet_cap_is_refused_with_how_to_make_room() -> None:
    service, store, _ = build()
    for i in range(MAX_WALLETS_PER_KIND):
        await reply(service, in_dm(f"my wallet is 0x{i + 1:040x}"))

    text = await reply(service, in_dm(f"minha carteira é {WALLET_TYPED}"))

    assert len(store.wallets[(LEO, FactKind.ETH_WALLET)]) == MAX_WALLETS_PER_KIND
    assert "você já tem 5 salvas" in text
    assert "esqueça minha carteira 0x" in text


async def test_forgetting_one_wallet_by_its_address_keeps_the_other() -> None:
    service, store, _ = build()
    await reply(service, in_dm(f"minha carteira é {WALLET_TYPED}"))
    await reply(service, in_dm(f"my wallet is {OTHER}"))

    text = await reply(service, in_channel(f"esqueça minha carteira {WALLET_TYPED}"))

    assert store.wallets[(LEO, FactKind.ETH_WALLET)] == [OTHER]
    assert text == "Pronto. Se essa carteira Ethereum estava salva, não está mais."
    assert WALLET not in text.lower()


@pytest.mark.parametrize("text", ["forget my wallets", "esqueça minhas carteiras"])
async def test_forgetting_the_wallets_forgets_every_one(text: str) -> None:
    service, store, _ = build()
    await reply(service, in_dm(f"minha carteira é {WALLET_TYPED}"))
    await reply(service, in_dm(f"my wallet is {OTHER}"))
    await reply(service, in_dm("call me Leo"))

    await reply(service, in_dm(text))

    assert (LEO, FactKind.ETH_WALLET) not in store.wallets
    assert store.rows[(LEO, FactKind.PREFERRED_NAME)] == "Leo"


def test_the_forget_replies_name_the_wallets_in_the_plural() -> None:
    assert fact_forgotten_reply(FactKind.ETH_WALLET, Language.PORTUGUESE) == (
        "Pronto. Não tenho mais suas carteiras Ethereum."
    )
    assert fact_forgotten_reply(FactKind.ETH_WALLET, Language.ENGLISH) == (
        "Done. I don't have your Ethereum wallets any more."
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (FactKind.HOME_ADDRESS, "seu endereço"),
        (FactKind.BIRTH_DATE, "sua data de nascimento"),
        (FactKind.ETH_WALLET, "sua carteira Ethereum"),
    ],
)
def test_portuguese_possessives_agree(kind: FactKind, expected: str) -> None:
    assert possessive(kind, Language.PORTUGUESE) == expected


def test_a_birth_date_is_shown_spelled_out() -> None:
    assert shown_value(FactKind.BIRTH_DATE, "1981-06-21", Language.ENGLISH) == "21 June 1981"
    assert shown_value(FactKind.BIRTH_DATE, "1981-06-21", Language.PORTUGUESE) == (
        "21 de junho de 1981"
    )


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("forget my address", FactKind.HOME_ADDRESS),
        ("esqueça minha data de nascimento", FactKind.BIRTH_DATE),
        ("forget my phone number", FactKind.PHONE),
        ("esqueça meu nome completo", FactKind.FULL_NAME),
    ],
)
def test_each_kind_can_be_forgotten_by_name(text: str, kind: FactKind) -> None:
    intent = fact_intent(text)
    assert intent is not None and intent.action is FactAction.FORGET
    assert intent.kind is kind and intent.value is None


# --- the prompt, and whoever reads the saved wallets ---------------------------------


def test_every_wallet_reaches_a_direct_message_prompt_whole() -> None:
    wallets = tuple(f"0x{i:040x}" for i in range(1, MAX_WALLETS_PER_KIND + 1))
    facts = AskerFacts(
        person=LEO, home_address=ADDRESS, birth_date="1981-06-21", eth_wallets=wallets
    )
    payload = _payload(None, facts)
    for wallet in wallets:
        assert f'"{wallet}"' in payload
    assert ADDRESS in payload and "1981-06-21" in payload


async def test_a_channel_prompt_holds_no_address_birth_date_or_wallet() -> None:
    service, _, answers = build()
    await reply(service, in_dm(PRODUCTION))

    await reply(service, in_channel("what did the team decide yesterday?"))

    system, user = answers.prompts[-1]
    assert "Leo" in user
    for private in ("Vargem", "1981", WALLET, EMAIL, "980703795"):
        assert private not in system + user


async def test_a_direct_message_prompt_holds_every_saved_fact() -> None:
    service, _, answers = build()
    await reply(service, in_dm(PRODUCTION))
    await reply(service, in_dm(f"my wallet is {OTHER}"))

    await reply(service, in_dm("what did the team decide yesterday?"))

    _, user = answers.prompts[-1]
    for private in (ADDRESS, "1981-06-21", WALLET, OTHER, EMAIL):
        assert private in user


async def test_egress_values_hold_every_saved_wallet() -> None:
    """Regression: only the first wallet of each kind was an authorised value,
    so a lookup of the second was refused by the egress guard."""
    service, _, answers = build()
    await reply(service, in_dm(f"minha carteira é {WALLET_TYPED}"))
    await reply(service, in_dm(f"my wallet is {OTHER}"))

    await reply(service, in_dm("what did the team decide yesterday?"))

    assert {WALLET, OTHER} <= answers.seen[-1].asker_values


def _lookup(text: str, *saved: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    query = crypto_route(text)
    assert query is not None, text
    found = _lookup_addresses(replace(question(text=text), asker_values=frozenset(saved)), query)
    return found.addresses, found.choose_from


def test_a_portfolio_sums_every_saved_wallet() -> None:
    assert _lookup("quanto eu tenho no total?", WALLET, OTHER) == ((WALLET, OTHER), ())


def test_a_balance_with_several_saved_wallets_asks_which() -> None:
    assert _lookup("qual o saldo da minha carteira?", WALLET, OTHER) == ((), (WALLET, OTHER))


def test_a_balance_names_one_of_several_by_its_last_characters() -> None:
    assert _lookup("qual o saldo da minha carteira …75e0?", WALLET, OTHER) == ((WALLET,), ())
    assert _lookup("what's my aave health factor on the one ending 6045?", WALLET, OTHER) == (
        (OTHER,),
        (),
    )


def test_one_saved_wallet_is_used_as_before() -> None:
    assert _lookup("qual o saldo da minha carteira?", WALLET) == ((WALLET,), ())


def test_a_suffix_never_introduces_an_address() -> None:
    assert named_by_suffix("the one ending 75e0", (OTHER,)) == ()
    # A bare hex word with no digit is a word, not a suffix.
    assert named_by_suffix("my cafe wallet", ("0x" + "0" * 36 + "cafe",)) == ()
    assert named_by_suffix("…cafe", ("0x" + "0" * 36 + "cafe",)) == ("0x" + "0" * 36 + "cafe",)
    assert suffix_list((WALLET, OTHER)) == "`…75e0`, `…6045`"


async def test_the_which_wallet_reply_lists_the_suffixes_and_reads_nothing() -> None:
    surface = ScriptedSurface(WALLET_TOOL, WEB, completion=WANTS_BALANCES)
    retrieval = FakeRetrieval([[SOMEBODY_ELSES_PROJECT]])
    asked = replace(
        question(text="what is my wallet balance?"), asker_values=frozenset({WALLET, OTHER})
    )

    outcome = await service(surface, retrieval, ParaphrasingSynthesizer()).answer_run(asked)

    assert outcome.answer.text == f"{WHICH_SAVED_WALLET}\n`…75e0`, `…6045`"
    assert surface.invocations == [] and retrieval.calls == []


async def test_a_stored_fact_result_for_a_new_wallet_is_stored() -> None:
    service = PersonalFactsService(FakeFactStore())

    result = await service.remember(viewer(LEO), FactKind.ETH_WALLET, WALLET_TYPED)
    assert result.outcome is FactOutcome.STORED
    assert result.fact is not None and result.fact.value == WALLET
