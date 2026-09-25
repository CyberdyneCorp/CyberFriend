"""Suggestions written as messages: the six forms, and every route outranking them.

The labelled cases at the bottom are the routing eval for this step: what a
message starting like a suggestion is answered as, through the ask service.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chatmemory.app import suggestion_intent as suggestion_intent_module
from chatmemory.app.ask import AskRequest
from chatmemory.app.language import Language
from chatmemory.app.limits import RateLimiter
from chatmemory.app.suggestion_intent import (
    PROPOSAL_QUOTE_CHARS,
    ROUTE_CLAIMS,
    SUGGESTION_FORMS,
    proposal_text,
    route_claiming,
    suggestion_intent,
)
from tests.unit.test_ask import GENERAL, LEAD, build, ch, person
from tests.unit.test_conversation_memory import FakeMemoryStore, in_general
from tests.unit.test_conversation_memory import service as remembering_service

PT, EN = Language.PORTUGUESE, Language.ENGLISH
NOW = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("message", "form", "stored", "language"),
    [
        ("tenho uma sugestão: modo escuro", "tenho uma sugestão", "modo escuro", PT),
        ("sugestão: modo escuro", "sugestão:", "modo escuro", PT),
        (
            "seria legal se você tivesse modo escuro",
            "seria legal se você",
            "seria legal se você tivesse modo escuro",
            PT,
        ),
        ("I have a feature request: dark mode", "I have a feature request", "dark mode", EN),
        ("feature request: dark mode", "feature request:", "dark mode", EN),
        (
            "it would be nice if you could use dark mode",
            "it would be nice if you could",
            "it would be nice if you could use dark mode",
            EN,
        ),
    ],
)
def test_each_form_is_recognised(message: str, form: str, stored: str, language: Language) -> None:
    intent = suggestion_intent(message)

    assert intent is not None
    assert intent.form.name == form
    assert intent.text == stored
    assert intent.language is language


def test_there_are_exactly_six_forms() -> None:
    assert len(SUGGESTION_FORMS) == 6


@pytest.mark.parametrize(
    "message",
    [
        "Tenho uma SUGESTAO - modo escuro",
        "<@123> sugestao : modo escuro",
        "  FEATURE REQUEST: dark mode",
        "Seria legal se voce tivesse modo escuro",
    ],
)
def test_case_accents_spacing_and_a_leading_mention_do_not_matter(message: str) -> None:
    intent = suggestion_intent(message)

    assert intent is not None
    assert "modo escuro" in intent.text or "dark mode" in intent.text


@pytest.mark.parametrize(
    "message",
    [
        "qual foi a sugestão do João?",
        "you should be able to tell me X",
        "sugestão",
        "sugiro que você tenha modo escuro",
        "você deveria ter modo escuro",
        "a sugestão: modo escuro",
        "eu tenho uma sugestão: modo escuro",
        "what feature request did Ana make?",
        "tenho uma sugestão",
        "feature request: ?!",
        "sugestões: modo escuro",
    ],
)
def test_no_other_wording_is_a_suggestion(message: str) -> None:
    assert suggestion_intent(message) is None


def test_the_remainder_is_what_follows_the_form() -> None:
    intent = suggestion_intent("it would be nice if you could show my portfolio")

    assert intent is not None
    assert intent.remainder == "show my portfolio"


#: One message per row of `ROUTE_CLAIMS`: each is claimed, so none could ever
#: be proposed as a suggestion.
CLAIMED = {
    "fact": "my name is Leo",
    "indexing": "index #design",
    "typed_command": "/forget",
    "alert": "notify me when BTC hits 100k",
    "catch_up": "o que eu perdi no <#123>?",
    "said_by": "what did Ana say about the deploy?",
    "self_description": "what can you do?",
    "obligations": "what did people ask me today?",
    "decisions": "what did we decide about the launch?",
    "mcp_change": "add the context7 mcp server",
    "market": "me diga o preço do BTC",
    "time": "what time is it?",
    "crypto": "show my portfolio",
    "web_search": "search the web for the latest python release",
}


def test_every_route_has_a_case() -> None:
    assert set(CLAIMED) == {name for name, _ in ROUTE_CLAIMS}


#: The modules that pick a route for a message, and the route predicates they
#: import that are not routes away from the corpus answer.
DISPATCHERS = ("ask.py", "reasoning/service.py")
PREDICATE_MODULES = frozenset(
    f"chatmemory.app.{name}"
    for name in (
        "routing",
        "routing_crypto",
        "alert_intent",
        "catchup",
        "said_by",
        "self_description",
    )
)
NOT_A_ROUTE = {
    "classify": "fixed path or loop, both the corpus answer",
    "typed_command_reply": "the reply to a typed command, not the check for one",
}


def _functions_imported_from_predicate_modules(source: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module in PREDICATE_MODULES:
            module = importlib.import_module(node.module)
            names |= {a.name for a in node.names if inspect.isfunction(getattr(module, a.name))}
    return names


def _called_in_route_claims() -> set[str]:
    tree = ast.parse(Path(suggestion_intent_module.__file__).read_text())
    [claims] = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "ROUTE_CLAIMS"
    ]
    return {
        node.func.id
        for node in ast.walk(claims)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_every_route_the_pipeline_dispatches_on_is_in_route_claims() -> None:
    """Not the list checked against itself: a route predicate the ask service
    or the reasoning service starts reading must be added to `ROUTE_CLAIMS`,
    or a suggestion would be proposed over what that route can answer."""
    app = Path(suggestion_intent_module.__file__).parent
    dispatched = set().union(
        *(
            _functions_imported_from_predicate_modules((app / module).read_text())
            for module in DISPATCHERS
        )
    )

    assert dispatched - set(NOT_A_ROUTE) <= _called_in_route_claims()
    assert set(NOT_A_ROUTE) <= dispatched, "an exclusion no dispatcher uses any more"


@pytest.mark.parametrize(("route", "message"), sorted(CLAIMED.items()))
def test_each_route_claims_its_own_message(route: str, message: str) -> None:
    assert route_claiming(message, now=NOW) == route


@pytest.mark.parametrize(
    "remainder",
    ["avisar quando alguém me marcar", "dark mode", "a weekly digest of our decisions"],
)
def test_a_genuine_suggestion_is_claimed_by_nothing(remainder: str) -> None:
    assert route_claiming(remainder, now=NOW) is None


def test_the_proposal_quotes_the_suggestion_on_one_line() -> None:
    long = "x " * PROPOSAL_QUOTE_CHARS

    text = proposal_text(f"modo\nescuro {long}", PT)

    assert "> modo escuro x" in text and text.count("\n") == 2
    assert "…" in text
    assert "Quer que eu registre para a equipe?" in text
    assert "Shall I record it for the team?" in proposal_text("dark mode", EN)


# --- the eval: what the ask service does with each ---------------------------------


async def ask(text: str, *, suggest: bool = True):  # type: ignore[no-untyped-def]
    service, answers = build()
    outcome = await service.ask(
        AskRequest(person(LEAD), text, ch(GENERAL), GENERAL), suggest=suggest
    )
    return outcome, answers


async def test_a_genuine_suggestion_is_proposed_and_nothing_is_answered() -> None:
    outcome, answers = await ask("tenho uma sugestão: avisar quando alguém me marcar")

    assert outcome.suggestion is not None
    assert outcome.suggestion.text == "avisar quando alguém me marcar"
    assert outcome.suggestion.language is PT
    assert outcome.suggestion.person == person(LEAD)
    assert outcome.scoped.answer.text == proposal_text("avisar quando alguém me marcar", PT)
    assert answers.seen == [], "a proposal reaches no answer service"


@pytest.mark.parametrize(
    "message", ["qual foi a sugestão do João?", "you should be able to tell me X"]
)
async def test_questions_with_suggestion_words_are_answered(message: str) -> None:
    outcome, answers = await ask(message)

    assert outcome.suggestion is None
    assert [q.text for q in answers.seen] == [message]


async def test_a_price_question_after_the_form_is_a_price_question() -> None:
    """The answer services see the whole message, and the market route reads it."""
    outcome, answers = await ask("sugestão: me diga o preço do BTC")

    assert outcome.suggestion is None
    assert [q.text for q in answers.seen] == ["sugestão: me diga o preço do BTC"]
    assert route_claiming(answers.seen[0].text) == "market"


async def test_a_follow_up_after_the_form_is_read_with_the_earlier_questions() -> None:
    """'e em euros?' is the market route only after a price question, so the
    asker's earlier questions must reach the check, not an empty history."""
    service, answers = remembering_service(FakeMemoryStore())
    await service.ask(in_general(LEAD, "qual o preço do BTC?"), suggest=True)

    outcome = await service.ask(in_general(LEAD, "sugestão: e em euros?"), suggest=True)

    assert route_claiming("e em euros?") is None, "claimed only because of what came before"
    assert outcome.suggestion is None
    assert [q.text for q in answers.seen] == ["qual o preço do BTC?", "e em euros?"]


async def test_a_portfolio_request_after_the_form_is_asked_as_those_words() -> None:
    outcome, answers = await ask("it would be nice if you could show my portfolio")

    assert outcome.suggestion is None
    assert [q.text for q in answers.seen] == ["show my portfolio"]


async def test_an_alert_request_after_the_form_is_the_alert_route() -> None:
    """Without alerts wired it is answered that alerts are unavailable, and
    still never proposed as a suggestion or searched."""
    outcome, answers = await ask("feature request: notify me when BTC hits 100k")

    assert outcome.suggestion is None
    assert answers.seen == []


async def test_without_suggest_the_message_is_answered_as_before() -> None:
    outcome, answers = await ask("tenho uma sugestão: modo escuro", suggest=False)

    assert outcome.suggestion is None
    assert [q.text for q in answers.seen] == ["tenho uma sugestão: modo escuro"]


async def test_answering_a_declined_proposal_spends_no_second_question() -> None:
    service, answers = build()
    service._limiter = RateLimiter(max_questions=1, window_seconds=60)
    request = AskRequest(person(LEAD), "sugestão: modo escuro", ch(GENERAL), GENERAL)

    proposed = await service.ask(request, suggest=True)
    answered = await service.answer_without_suggestion(request)

    assert proposed.suggestion is not None
    assert answered.suggestion is None and not answered.rate_limited
    assert [q.text for q in answers.seen] == ["sugestão: modo escuro"]
