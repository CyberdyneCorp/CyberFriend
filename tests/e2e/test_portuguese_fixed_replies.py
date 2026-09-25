"""S12: a fixed reply to a Portuguese question is in Portuguese.

The production failure: the answer-language rule is applied as a lookup of
known English strings at the end of `AskService.ask`, so any fixed reply
emitted elsewhere -- or added without a table entry -- reaches a Portuguese
speaker in English. Unit tests asserted on `AskOutcome` or on the table; what
is asserted here is the string Discord received.

The known-open replies are strict xfails naming their constant: the suite is
green, the debt is listed, and fixing one turns its xfail into a failure that
forces the marker off. `NOTHING_FOUND` is localised today and guards the table.

Each xfail expects `LanguageMismatch` and nothing else, and first checks that
the reply sent *is* the constant it names. A broken precondition raises
`ScenarioBroken`, and a harness violation (a command not offered, an
unscripted host) raises its own error; neither is a `LanguageMismatch`, so
both fail instead of hiding inside an expected failure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from chatmemory.adapters.discord.formatting import split_message
from chatmemory.app.catchup import NAME_THE_CHANNEL
from chatmemory.app.language import Language
from chatmemory.app.localise import PORTUGUESE
from chatmemory.app.reasoning.contract import NOTHING_FOUND
from chatmemory.app.reasoning.service import MCP_CHANGE_REFUSAL
from tests.e2e.harness.conversation import E2EBot, LanguageMismatch, Turn


class ScenarioBroken(RuntimeError):
    """The scenario no longer exercises the reply it names."""


def precondition(holds: bool, what: str) -> None:
    if not holds:
        raise ScenarioBroken(what)


def open_item(constant: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(
        strict=True, raises=LanguageMismatch, reason=f"{constant} is English-only"
    )


async def test_a_corpus_miss_is_answered_in_portuguese(bot: E2EBot) -> None:
    dm = bot.dm(bot.person("Ana"))

    turn = await dm.say("o que aconteceu com o foguete da equipe de marte?")

    precondition(turn.searched, "the question no longer reaches the corpus")
    assert turn.text == PORTUGUESE[NOTHING_FOUND]
    turn.assert_language("pt")


async def test_a_bare_mention_from_a_portuguese_speaker(bot: E2EBot) -> None:
    ana = bot.person("Ana")
    await bot.dm(ana).say("fale comigo em português")
    facts = await bot.facts_of(ana)
    precondition(facts.get("preferred_language") is not None, "the language was not kept")

    turn = await bot.channel("general", ana).mention_only()

    precondition(bool(turn.sent) and not turn.searched, "a bare mention no longer replies")
    described = bot.process.stack.capabilities.describe(Language.PORTUGUESE)
    assert turn.text == "\n".join(split_message(described))
    turn.assert_language("pt")


async def _say_in_dm(bot: E2EBot, text: str) -> Turn:
    return await bot.dm(bot.person("Ana")).say(text)


async def _say_in_general(bot: E2EBot, text: str) -> Turn:
    return await bot.channel("general", bot.person("Ana")).say(text)


Say = Callable[[E2EBot, str], Awaitable[Turn]]


@pytest.mark.parametrize(
    ("say", "text", "reply"),
    [
        pytest.param(
            _say_in_dm,
            "adicione um servidor MCP de issues",
            MCP_CHANGE_REFUSAL,
            id="mcp-change-refusal",
        ),
        pytest.param(
            _say_in_general,
            "resuma o que perdi em #inexistente",
            NAME_THE_CHANNEL,
            id="catch-up-refusal",
        ),
    ],
)
async def test_a_fixed_refusal_to_a_portuguese_question(
    bot: E2EBot, say: Say, text: str, reply: str
) -> None:
    turn = await say(bot, text)

    precondition(turn.edge() == "NONE", f"{text!r} no longer gets a fixed reply: {turn}")
    assert turn.text.strip() == PORTUGUESE[reply].strip()
    turn.assert_language("pt")
