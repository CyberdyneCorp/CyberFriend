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

from chatmemory.app.egress import (
    MARKET_CRYPTO_PROVIDER,
    MARKET_FX_PROVIDER,
    MARKET_INDEX_PROVIDER,
)
from chatmemory.app.routing import self_description_question
from chatmemory.ports.answers import Answer, AnswerService, Question

MARKET_DESCRIPTIONS = {
    MARKET_CRYPTO_PROVIDER: "Bitcoin and Ether prices",
    MARKET_INDEX_PROVIDER: "the S&P 500",
    MARKET_FX_PROVIDER: "currency conversion (daily reference rates)",
}
"""What each market server lets a person ask, in the order they are listed.

Keyed by server, so a deployment without a SerpApi key -- and therefore
without the S&P 500 -- does not promise it."""


def describe_capabilities(
    external_tools: Sequence[str], *, personal_facts: bool = False
) -> str:
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
    servers = {t.split(":")[0] for t in external_tools}
    market = [text for server, text in MARKET_DESCRIPTIONS.items() if server in servers]
    other = sorted(
        t for t in external_tools if t not in web and t.split(":")[0] not in MARKET_DESCRIPTIONS
    )
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
    if market:
        lines += [
            "",
            "Current market data, from live sources and never from a price "
            "somebody mentioned in a channel: " + ", ".join(market) + ".",
            "Each figure says how current it is. I report figures; I don't "
            "recommend buying, selling or holding anything.",
        ]
    if personal_facts:
        lines += [
            "",
            "Tell me what to call you (`call me Leo`), your email address, or "
            "the language you'd like answers in, and I'll remember it. Ask "
            "`what do you know about me?` to see it; I only show your email to "
            "you, in a direct message.",
        ]
    lines += [
        "",
        "In a channel, I only cite what everyone there can read. Ask me in a "
        "direct message for your full view.",
        "Use `/resolve` to mark something you were asked to do as done.",
    ]
    return "\n".join(lines)


class SelfDescriptionAnswerService:
    """Answers questions about the assistant itself; delegates the rest."""

    def __init__(
        self,
        fallback: AnswerService,
        external_tools: Sequence[str] = (),
        *,
        personal_facts: bool = False,
    ) -> None:
        self._fallback = fallback
        self._description = describe_capabilities(external_tools, personal_facts=personal_facts)

    async def answer(self, question: Question) -> Answer:
        if self_description_question(question.text):
            # No citations, deliberately: nothing here came from a message,
            # and inventing a source for a description of configuration would
            # make it look retrieved.
            return Answer(self._description)
        return await self._fallback.answer(question)
