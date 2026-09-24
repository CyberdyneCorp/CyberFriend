"""Personal facts in the asker's language, and the questions that missed.

From production, after introductions shipped. Every one of these was either
answered in English to a Portuguese message, or sent to the corpus:

    /forget                                   -> "Não há evidência suficiente..."
    Oi me chamo Leonardo Araujo, pode me ...   -> "Here's what I saved:" (no full name)
    Oque voce sabe sobre mim?                  -> "Here's what you've asked me to remember:"
    Qual o meu telefone ?                      -> "I couldn't find anything about that..."
    what's my Phone Number ?                   -> "I couldn't find anything about that..."
    Que informacoes voce pode guardar sobre mim ? -> an answer from channel messages
"""

from __future__ import annotations

import pytest

from chatmemory.app.fact_replies import possessive
from chatmemory.app.language import Language, detect
from chatmemory.app.reasoning.contract import NOTHING_FOUND
from chatmemory.ports.facts import FactKind, PersonalFact
from tests.unit.test_facts_behaviour import (
    LEO,
    RecordingAnswers,
    build,
    in_channel,
    in_dm,
    reply,
)

PHONE = "+55 21 980703795"


async def _with_phone() -> tuple[object, object, RecordingAnswers]:
    service, store, answers = build()
    await store.set_fact(LEO, PersonalFact(FactKind.PHONE, PHONE))
    await store.set_fact(LEO, PersonalFact(FactKind.PREFERRED_NAME, "Leo"))
    return service, store, answers


async def test_what_it_knows_is_answered_in_portuguese() -> None:
    service, _, answers = await _with_phone()
    text = await reply(service, in_dm("Oque voce sabe sobre mim?"))  # type: ignore[arg-type]
    assert text.startswith("Aqui está o que você me pediu para guardar:")
    assert "• Telefone: **" + PHONE + "**" in text
    assert answers.seen == []


@pytest.mark.parametrize(
    ("question", "line"),
    [
        ("Qual o meu telefone ?", f"• Telefone: **{PHONE}**"),
        ("what's my Phone Number ?", f"• Phone number: **{PHONE}**"),
    ],
)
async def test_one_fact_asked_for_is_answered_from_the_store(question: str, line: str) -> None:
    service, _, answers = await _with_phone()
    text = await reply(service, in_dm(question))  # type: ignore[arg-type]
    assert text == line
    assert answers.seen == [], "the corpus was searched for the asker's own phone"


async def test_in_a_channel_one_contact_fact_is_not_shown_or_confirmed() -> None:
    """The same words whether or not a phone is stored."""
    service, _, _ = await _with_phone()
    stored = await reply(service, in_channel("Qual o meu telefone ?"))  # type: ignore[arg-type]
    empty_service, _, _ = build()
    missing = await reply(empty_service, in_channel("Qual o meu telefone ?"))
    assert PHONE not in stored
    assert stored == missing == "Só mostro seu telefone em mensagem direta. Me pergunte lá."


async def test_asking_for_a_name_with_only_a_preferred_name_gives_it() -> None:
    service, _, _ = await _with_phone()
    assert await reply(service, in_dm("qual o meu nome?")) == "• Nome preferido: **Leo**"  # type: ignore[arg-type]


async def test_what_can_be_remembered_is_listed_not_searched() -> None:
    service, _, answers = build()
    text = await reply(service, in_dm("Que informacoes voce pode guardar sobre mim ?"))
    assert text.startswith("Posso guardar estas informações sobre você")
    assert answers.seen == []


async def test_me_chamo_is_a_full_name() -> None:
    service, store, _ = build()
    text = await reply(
        service,
        in_dm("Oi me chamo Leonardo Araujo, pode me chamar de Leo, eu moro no Brasil em "
              "vargem grande"),
    )
    assert store.rows[(LEO, FactKind.FULL_NAME)] == "Leonardo Araujo"
    assert store.rows[(LEO, FactKind.PREFERRED_NAME)] == "Leo"
    assert "Nome completo: **Leonardo Araujo**" in text


@pytest.mark.parametrize("typed", ["/forget", "/schedule list", " /channels "])
async def test_a_slash_command_typed_as_text_says_how_to_run_it(typed: str) -> None:
    service, store, answers = build()
    text = await reply(service, in_dm(typed))
    name = typed.strip().lstrip("/")
    assert f"**/{name}**" in text
    assert "Digite `/`" in text and "Type `/`" in text  # no language to go by: both
    assert answers.seen == []


async def test_the_no_answer_reply_is_given_in_portuguese() -> None:
    service, _, _ = build(answers=RecordingAnswers(text=NOTHING_FOUND))
    text = await reply(service, in_dm("o que foi decidido sobre o deploy?"))
    assert text == "Não encontrei nada sobre isso nas mensagens que você pode ver."
    english = await reply(service, in_dm("what was decided about the deploy?"))
    assert english == NOTHING_FOUND


def test_a_portuguese_name_does_not_make_an_english_question_portuguese() -> None:
    assert detect("what's João's email?") is Language.ENGLISH
    assert detect("João's email is joao@example.com") is not Language.PORTUGUESE
    assert detect("não sei") is Language.PORTUGUESE


def test_portuguese_possessives_agree_with_the_noun() -> None:
    assert possessive(FactKind.ETH_WALLET, Language.PORTUGUESE) == "sua carteira Ethereum"
    assert possessive(FactKind.PHONE, Language.PORTUGUESE) == "seu telefone"
    assert possessive(FactKind.PHONE, Language.ENGLISH) == "your phone number"


@pytest.mark.parametrize(
    ("saved", "language"),
    [("Portuguese", Language.PORTUGUESE), ("português", Language.PORTUGUESE),
     ("pt-BR", Language.PORTUGUESE), ("English", Language.ENGLISH), ("Klingon", Language.UNKNOWN),
     (None, Language.UNKNOWN)],
)
def test_a_saved_language_preference_is_read_as_a_language(
    saved: str | None, language: Language
) -> None:
    """A bare mention has no words to detect; the saved preference decides."""
    from chatmemory.app.language import language_named

    assert language_named(saved) is language


def test_a_portuguese_request_with_only_articles_is_portuguese() -> None:
    """ "adicione um servidor MCP de issues" was UNKNOWN, so the MCP refusal
    reached a Portuguese speaker in English."""
    assert detect("adicione um servidor MCP de issues") is Language.PORTUGUESE
    assert detect("de facto standard for the index") is Language.ENGLISH
