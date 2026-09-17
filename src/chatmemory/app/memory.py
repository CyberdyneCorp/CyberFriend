"""Conversation memory: what is remembered, and on what provenance.

The store decides what a person may be reminded of (see `ports.memory`). This
module decides what may be *written*, and its whole job is working out which
channels an answer drew on -- because that record is the only thing that later
stops a remembered answer from replaying a channel the person has since lost.

Provenance is taken from three places and unioned:

*   the answer's citations, as produced -- before any audience guard drops
    some for delivery. A citation dropped from a public reply was still
    evidence the model read and may have paraphrased;
*   every channel the answering run consulted, cited or not. An answer can
    carry an uncited sentence from a window it was shown, and a provenance
    built from citations alone would let that sentence outlive a revocation;
    and
*   every channel behind the memory the run was shown. A follow-up can restate
    a remembered answer -- "about that (the layoff): #general only mentions a
    lunch" -- while its own evidence is elsewhere. Recorded with its own
    evidence alone, that turn would carry the remembered content past the
    revocation of the channel it came from; recorded with web evidence alone,
    its provenance would be empty and would pass every check there is.

It fails closed. Corpus evidence is recorded by channel; web results belong to
no channel and have no permission to revoke; anything else -- a source system
added later, a federated tool with its own access rules -- has a readability
nobody here can re-check, so a turn that drew on it is not remembered at all.
Losing a turn costs a follow-up some context. Storing one whose provenance is
wrong costs a disclosure.

Remembered text is DATA. This module never renders it into a prompt, and
nothing it returns may be cited.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from chatmemory.app.reasoning.evidence import CORPUS_SOURCE_SYSTEMS, SOURCE_WEB
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer
from chatmemory.ports.facts import PersonFactEraser
from chatmemory.ports.memory import (
    ConversationLocation,
    MemoryPurge,
    MemoryStore,
    Recollection,
)

log = structlog.get_logger()

DEFAULT_RECENT_TURNS = 6
"""How many permitted turns a recollection carries verbatim."""


def answer_provenance(
    location: ConversationLocation,
    answer: Answer,
    consulted: Iterable[ChannelRef],
    informed_by: Recollection,
) -> frozenset[ChannelRef] | None:
    """The channels a turn drew on, or None when that cannot be established.

    None is not "no channels": an empty set is a positive claim that the answer
    rests on nothing channel-scoped, and it passes every later check.
    """
    remembered = informed_by.source_channels
    if any(c.platform != location.platform for c in remembered):
        # The store only ever returns this location's platform; anything else
        # is a provenance nobody here can re-check.
        return None
    channels: set[ChannelRef] = set(remembered)
    for citation in answer.citations:
        if citation.source_system in CORPUS_SOURCE_SYSTEMS:
            channels.add(citation.channel)
        elif citation.source_system != SOURCE_WEB:
            return None
    for channel in consulted:
        if channel.platform == location.platform:
            channels.add(channel)
        elif channel.platform != SOURCE_WEB:
            # External evidence is filed under `ChannelRef(source_system, 0)`;
            # only the web is known to carry no access rule of its own.
            return None
    return frozenset(channels)


class ConversationMemory:
    """Records a person's turns and recalls them under their current access."""

    def __init__(
        self,
        store: MemoryStore,
        recent_turns: int = DEFAULT_RECENT_TURNS,
        *,
        facts: PersonFactEraser | None = None,
    ) -> None:
        if recent_turns <= 0:
            raise ValueError("recent_turns must be positive")
        self._store = store
        self._recent_turns = recent_turns
        # Personal facts share `/forget` everywhere with conversation history
        # (openspec personal-facts). Optional only so existing constructions
        # keep working; a forget-everywhere without it logs an error below.
        self._facts = facts

    async def recall(self, viewer: Viewer, location: ConversationLocation) -> Recollection:
        """The viewer's own conversation here, with anything no longer readable removed.

        The viewer is required: whose memory is read, and what survives the
        permission check, are both decided by it.
        """
        return await self._store.recall(viewer, location, self._recent_turns)

    async def remember(
        self,
        viewer: Viewer,
        location: ConversationLocation,
        question: str,
        answer: Answer,
        *,
        consulted: Iterable[ChannelRef],
        informed_by: Recollection,
    ) -> bool:
        """Store a turn. Returns whether it was stored.

        `consulted` and `informed_by` are keyword-only and have no default, so
        a caller cannot silently fall back to a provenance that leaves out the
        evidence the run read or the memory it was shown.
        """
        provenance = answer_provenance(location, answer, consulted, informed_by)
        if provenance is None:
            log.info(
                "memory.turn_not_stored",
                person=str(viewer.person),
                location=str(location),
                reason="unverifiable_provenance",
            )
            return False
        return await self._store.record_turn(
            viewer.person, location, question, answer.text, provenance
        )

    async def forget(
        self, person: PersonRef, location: ConversationLocation | None
    ) -> MemoryPurge:
        """Forget history here, or everywhere when `location` is None.

        Everywhere also deletes the person's facts: a fact belongs to the
        person rather than to a location, so only the unscoped forget reaches
        it. Facts go first -- they are the longer-lived personal data, and a
        failure between the two deletes should leave history, not an email.
        """
        if location is None:
            await self._forget_facts(person)
        return await self._store.forget(person, location)

    async def _forget_facts(self, person: PersonRef) -> None:
        if self._facts is None:
            # Loud, because "forgot everything" that kept an email address is
            # the failure an operator has no other way to see.
            log.error(
                "memory.facts_not_covered",
                person=str(person),
                hint="no fact store wired; /forget everywhere left personal facts",
            )
            return
        count = await self._facts.forget_all_facts(person)
        log.info("memory.facts_forgotten", person=str(person), facts=count)
