"""Which questions the decision route claims, and what it reads out of them.

A labelled set in both languages. The negatives matter as much as the
positives: a question this claims wrongly is answered from the decision log
instead of by retrieval, so "what did you decide", "decide between A and B"
and every question another route owns have to stay unclaimed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from chatmemory.app.catchup import catch_up_request
from chatmemory.app.language import Language
from chatmemory.app.routing import decision_question, obligation_question
from chatmemory.app.said_by import said_by_request
from chatmemory.app.self_description import _TEXT as CAPABILITIES

TZ = ZoneInfo("America/Sao_Paulo")
#: Tuesday 1 September 2026, 12:00 UTC -- 09:00 in Sao Paulo.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

PT = Language.PORTUGUESE
EN = Language.ENGLISH


def parse(text: str) -> tuple[str, str | None, Language] | None:
    asked = decision_question(text, NOW, TZ)
    if asked is None:
        return None
    return asked.topic, asked.span.label if asked.span else None, asked.language


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("o que decidimos sobre o deploy?", ("o deploy", None, PT)),
        ("O que ficou decidido sobre o deploy?", ("o deploy", None, PT)),
        ("o que foi decidido sobre o Postgres semana passada?", ("o Postgres", "last_week", PT)),
        ("o que a gente decidiu do banco?", ("banco", None, PT)),
        ("o que o time decidiu sobre o deploy?", ("o deploy", None, PT)),
        ("o que decidimos sobre o C#?", ("o C#", None, PT)),
        ("e o que nós combinamos sobre a migração?", ("a migração", None, PT)),
        ("o que ficou definido ontem sobre o release", ("o release", "yesterday", PT)),
        ("qual foi a decisão sobre o preço?", ("o preço", None, PT)),
        ("quais foram as decisões sobre o onboarding?", ("o onboarding", None, PT)),
        ("que decisões tomamos sobre o deploy?", ("o deploy", None, PT)),
        ("teve alguma decisão sobre o deploy?", ("o deploy", None, PT)),
        ("decisões sobre o deploy?", ("o deploy", None, PT)),
        ("o que decidimos esta semana?", ("", "this_week", PT)),
        ("o que foi decidido?", ("", None, PT)),
        ("what did we decide about the deploy?", ("the deploy", None, EN)),
        ("What was decided about the deploy last week?", ("the deploy", "last_week", EN)),
        ("what have we agreed on regarding pricing?", ("pricing", None, EN)),
        ("what did we settle on for the release", ("the release", None, EN)),
        ("what did we decide to do about the outage?", ("the outage", None, EN)),
        ("what did we agree on?", ("", None, EN)),
        ("what's the decision on pricing?", ("pricing", None, EN)),
        ("what were our decisions about hiring?", ("hiring", None, EN)),
        ("what decisions did we make about the roadmap?", ("the roadmap", None, EN)),
        ("did we decide anything about the migration?", ("the migration", None, EN)),
        ("were there any decisions on the rollout yesterday?", ("the rollout", "yesterday", EN)),
        ("decisions about the deploy?", ("the deploy", None, EN)),
    ],
)
def test_decision_questions_are_claimed(
    text: str, expected: tuple[str, str | None, Language]
) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Asks the bot, or one person, not what the group settled.
        "what did you decide?",
        "what did you decide about the deploy?",
        "o que você decidiu sobre o deploy?",
        "o que o João decidiu sobre o deploy?",
        # Portuguese drops the subject: a bare singular is "you", not "we".
        "o que decidiu?",
        "o que decidiu sobre o deploy?",
        # Asks for help choosing.
        "help me decide between Postgres and MySQL",
        "what did we decide between Postgres and MySQL?",
        "o que decidimos entre Postgres e MySQL?",
        "should we decide on the deploy today?",
        "what should we decide about the deploy?",
        # A pronoun needs the conversation, which retrieval has.
        "o que decidimos sobre isso?",
        "what did we decide about it?",
        "what did we decide about me?",
        "o que decidimos sobre mim?",
        "o que decidimos sobre você?",
        # A topic-less follow-up continues a conversation retrieval has.
        "e o que decidimos?",
        "and what did we decide?",
        "so what was decided?",
        # A channel is a place, not a topic.
        "o que decidiram no #leadership?",
        "what did we decide about <#123>?",
        "what did we decide about the deploy in #general?",
        # Two lookups, or a range no single span covers: retrieval's.
        "what did we decide about the deploy and who is doing it?",
        "o que decidimos de segunda a quarta?",
        # A bare word is not a question about the log.
        "decisions?",
        "decisão",
        # A current market figure is the market route's.
        "any decisions on the btc price?",
        # Other routes' questions.
        "what did Ana say about the deploy?",
        "o que o João disse sobre o deploy semana passada?",
        "what did I miss in #general?",
        "o que eu perdi?",
        "what did people ask me today?",
        "what do I need to do?",
        "what do you know about me?",
        "what is the bitcoin price?",
        "we decided to ship on friday",
    ],
)
def test_everything_else_is_left_alone(text: str) -> None:
    assert decision_question(text, NOW, TZ) is None


@pytest.mark.parametrize(
    "text",
    ["o que decidimos sobre o deploy?", "what did we decide about the deploy last week?"],
)
def test_no_other_route_claims_a_decision_question(text: str) -> None:
    """Catch-up and said-by run before the decision route; obligations in front."""
    assert catch_up_request(text) is None
    assert said_by_request(text, NOW, TZ) is None
    assert obligation_question(text) is None


def test_the_span_is_a_calendar_week_in_the_deployment_zone() -> None:
    asked = decision_question("o que decidimos sobre o deploy semana passada?", NOW, TZ)
    assert asked is not None and asked.span is not None
    # Monday 24 August to Monday 31 August, Sao Paulo midnights.
    assert asked.span.start == datetime(2026, 8, 24, 3, tzinfo=UTC)
    assert asked.span.end == datetime(2026, 8, 31, 3, tzinfo=UTC)


def test_the_topic_keeps_what_was_typed() -> None:
    asked = decision_question("O que decidimos sobre a Migração do Banco?", NOW, TZ)
    assert asked is not None
    assert asked.topic == "a Migração do Banco"


def test_deciding_costs_no_model_call() -> None:
    asked = decision_question("what did we decide about the deploy?", NOW, TZ)
    assert asked is not None and asked.model_calls == 0


def test_the_decision_example_the_bot_offers_is_one_it_answers() -> None:
    """"What can you do" suggests a decision question in both languages; each
    has to reach the decision route, or the bot advertises a lookup it skips."""
    for language, words in CAPABILITIES.items():
        offered = [a for a in words["asks"] if "decid" in a]
        assert offered, f"no decision example in {language}"
        for example in offered:
            asked = decision_question(example, NOW, TZ)
            assert asked is not None and asked.language is language, example
