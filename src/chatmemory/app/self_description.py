"""Answering "what can you do?" from configuration, never from the corpus.

The corpus is other people's conversations. Searching it for the assistant's
own capabilities finds whatever somebody once wrote about some other tool, and
the answer then presents that as fact about itself: asked what it could do,
the bot replied that it calculates cryptocurrency prices, because a colleague
had described a project that did.

So a self-description is assembled from what this deployment is actually
running. It names the external tools only when they are registered, which
keeps it honest in the other direction too -- a capability list that promises
web search on a deployment without it is the same mistake inverted.
"""

from __future__ import annotations

from collections.abc import Sequence

from chatmemory.app.routing import self_description_question
from chatmemory.ports.answers import Answer, AnswerService, Question


def describe_capabilities(external_tools: Sequence[str]) -> str:
    lines = [
        "I'm CyberFriend. I answer questions about what has been said in the "
        "channels you're allowed to read, and I cite the messages I used.",
        "",
        "Things you can ask me:",
        "• what did people ask me to do today?",
        "• what was decided about the deploy last week?",
        "• summarise the discussion in #general yesterday",
    ]
    web = sorted(t for t in external_tools if t.split(":")[0] in {"wikipedia", "serpapi"})
    other = sorted(t for t in external_tools if t not in web)
    if web or other:
        lines += ["", "When the conversations don't have the answer, I can also look it up:"]
        if web:
            lines.append("• the web (" + ", ".join(sorted({t.split(":")[0] for t in web})) + ")")
        if other:
            lines.append("• " + ", ".join(sorted({t.split(":")[0] for t in other})))
        lines.append(
            "Answers from outside this server are labelled as such, so you can "
            "always tell what your colleagues said from what I looked up."
        )
    lines += [
        "",
        "In a channel, I only cite what everyone there can read. Ask me in a "
        "direct message for your full view.",
        "Use `/resolve` to mark something you were asked to do as done.",
    ]
    return "\n".join(lines)


class SelfDescriptionAnswerService:
    """Answers questions about the assistant itself; delegates the rest."""

    def __init__(self, fallback: AnswerService, external_tools: Sequence[str] = ()) -> None:
        self._fallback = fallback
        self._description = describe_capabilities(external_tools)

    async def answer(self, question: Question) -> Answer:
        if self_description_question(question.text):
            # No citations, deliberately: nothing here came from a message,
            # and inventing a source for a description of configuration would
            # make it look retrieved.
            return Answer(self._description)
        return await self._fallback.answer(question)
