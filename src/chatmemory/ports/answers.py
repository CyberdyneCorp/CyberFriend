"""The answer-producing port.

The Discord surface depends on this protocol, not on the reasoning
implementation. That keeps the surface testable before retrieval exists, and
lets the reasoning change supply a real adapter without touching this code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from chatmemory.domain.audience import Audience
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.memory import Recollection


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
    # Every channel the run that produced this answer consulted, cited or not.
    # Conversation memory records it as the answer's provenance, because an
    # uncited sentence can still paraphrase a window the run was shown. None
    # means nobody established it, and such an answer is not remembered: an
    # empty set would be a claim that the answer rests on no channel at all.
    consulted_channels: frozenset[ChannelRef] | None = None

    @property
    def source_channels(self) -> frozenset[ChannelRef]:
        return frozenset(c.channel for c in self.citations)


@dataclass(frozen=True, slots=True)
class AskerProfile:
    """What the asker's own Discord profile says about them.

    Carries its `person` so a renderer can check it belongs to the asker
    before it reaches a prompt: a profile is only ever context about the
    person consenting to it by asking, never a description of someone else.
    There is deliberately no join date or activity here -- nothing that would
    make this a dossier if it were ever built for the wrong person.

    Every text field is typed by a member of the server and is untrusted.
    """

    person: PersonRef
    display_name: str
    nickname: str | None
    role_names: tuple[str, ...]


class AskerProfileResolver(Protocol):
    async def resolve_profile(self, person: PersonRef) -> AskerProfile | None:
        """The person's own profile, or None when it cannot be resolved.

        Must not raise: a missing profile means answering without one.
        """
        ...


@dataclass(frozen=True, slots=True)
class Question:
    text: str
    asker: Viewer
    audience: Audience
    # The asker's own earlier turns in this location, already filtered by what
    # they may read now. Context for interpreting the question, never evidence:
    # nothing in it carries a window id, so nothing in it can become a citation.
    memory: Recollection = Recollection()
    # Optional so every existing construction keeps working, and because an
    # unresolvable profile is a reason to answer without it, never to fail.
    asker_profile: AskerProfile | None = None


class AnswerService(Protocol):
    async def answer(self, question: Question) -> Answer:
        """Produce an answer bounded by `question.audience`.

        Implementations must scope retrieval by the audience, not the asker.
        """
        ...
