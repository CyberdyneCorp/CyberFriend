"""The answer-producing port.

The Discord surface depends on this protocol, not on the reasoning
implementation. That keeps the surface testable before retrieval exists, and
lets the reasoning change supply a real adapter without touching this code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from chatmemory.domain.audience import Audience
from chatmemory.domain.identity import ChannelRef, Viewer


@dataclass(frozen=True, slots=True)
class Citation:
    channel: ChannelRef
    message_id: int
    author_display: str
    excerpt: str
    url: str
    # Where this came from. Defaults to the corpus, so any citation that has
    # not thought about provenance is treated as channel-scoped -- the
    # conservative direction.
    source_system: str = "discord"

    @property
    def is_corpus(self) -> bool:
        """Whether channel permissions decide who may see this.

        Only corpus evidence is channel-scoped. A web result belongs to no
        channel, so judging it by channel membership refuses it always --
        and because a dropped citation suppresses the whole answer, a single
        correctly-labelled web citation would silence every reply.
        """
        return self.source_system == "discord"


@dataclass(frozen=True, slots=True)
class Answer:
    """An answer and the evidence behind it.

    `withheld_channels` records sources the asker could have seen but the
    audience could not. It drives the private notice, and must never be
    rendered into a public reply.
    """

    text: str
    citations: tuple[Citation, ...] = ()
    abstained: bool = False
    partial: bool = False
    withheld_channels: frozenset[ChannelRef] = field(default_factory=frozenset)

    @property
    def source_channels(self) -> frozenset[ChannelRef]:
        return frozenset(c.channel for c in self.citations)


@dataclass(frozen=True, slots=True)
class Question:
    text: str
    asker: Viewer
    audience: Audience
    history: tuple[str, ...] = ()


class AnswerService(Protocol):
    async def answer(self, question: Question) -> Answer:
        """Produce an answer bounded by `question.audience`.

        Implementations must scope retrieval by the audience, not the asker.
        """
        ...
