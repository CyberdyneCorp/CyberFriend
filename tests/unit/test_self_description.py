"""The assistant describes itself from configuration, never from the corpus."""

from __future__ import annotations

import pytest

from chatmemory.app.routing import self_description_question
from chatmemory.app.self_description import (
    SelfDescriptionAnswerService,
    describe_capabilities,
)
from chatmemory.domain.audience import private_audience
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer, Question

ASKER = PersonRef("discord", 1)
GENERAL = ChannelRef("discord", 10)


class Recording:
    """A fallback that records whether it was consulted."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def answer(self, question: Question) -> Answer:
        self.asked.append(question.text)
        # What the corpus actually said about "capabilities": somebody else's
        # project. This is the answer the bot must never give about itself.
        return Answer("I can calculate cryptocurrency prices.")


def q(text: str) -> Question:
    visible = frozenset({GENERAL})
    return Question(
        text=text,
        asker=Viewer(ASKER, visible),
        audience=private_audience(ASKER, visible),
    )


@pytest.mark.parametrize(
    "text",
    [
        "What you can do ?", "what can you do?", "who are you", "o que você faz?",
        "quem é você", "how do you work",
        # Missed by the phrase list. Each one sent the bot to its channels,
        # where it found a colleague's crypto project and claimed its features.
        "what tools are available?",
        "What about Wikipedia, Internet access, or MCP ?",
        "do you have internet access",
        "quais ferramentas você tem?",
        "can you use MCP",
    ],
)
def test_self_description_questions_are_recognised(text: str) -> None:
    assert self_description_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "who wrote the novel Dune ?",
        "what did people ask me to do today",
        # Contains the phrase, and is about something else entirely.
        "what can you do about the deploy pipeline failing on staging every night",
        # About the TEAM's tools, not the assistant's.
        "what tools did the team decide to use for the migration last week",
        "when is the espresso machine repair",
    ],
)
def test_other_questions_are_not(text: str) -> None:
    """A false positive replaces a real question with a capability list,
    which is the worse of the two mistakes."""
    assert not self_description_question(text)


async def test_what_can_you_do_never_reaches_the_corpus() -> None:
    """Searching other people's conversations for the bot's capabilities
    found a colleague's crypto project, and the bot claimed its features."""
    fallback = Recording()
    service = SelfDescriptionAnswerService(fallback)

    answer = await service.answer(q("What you can do ?"))

    assert fallback.asked == [], "a self-description question reached retrieval"
    assert "cryptocurrency" not in answer.text
    assert "CyberFriend" in answer.text


async def test_a_self_description_cites_nothing() -> None:
    """Nothing in it came from a message; a citation would make it look retrieved."""
    answer = await SelfDescriptionAnswerService(Recording()).answer(q("who are you"))
    assert answer.citations == ()


async def test_other_questions_still_reach_the_fallback() -> None:
    fallback = Recording()
    await SelfDescriptionAnswerService(fallback).answer(q("who wrote the novel Dune"))
    assert fallback.asked == ["who wrote the novel Dune"]


def test_web_search_is_only_claimed_when_it_is_configured() -> None:
    """Promising a capability the deployment lacks is the same mistake inverted."""
    assert "web" not in describe_capabilities(())
    described = describe_capabilities(["wikipedia:search", "serpapi:search"])
    assert "web" in described
    assert "labelled" in described, "must say outside answers are marked as such"
