"""Personal facts: setting them, and deciding where they may be shown.

The store decides whose facts are read (see `ports.facts`). This module decides
two things the store cannot:

*   **Who a fact is about.** Every write takes the asker's `Viewer` and writes
    to that viewer's person. There is no argument naming anyone else, so "João's
    email is ..." has nowhere to go but the asker's own record -- and the intent
    layer that recognises it as a statement about someone else refuses it before
    it gets here. What reaches `remember` is, by construction, the asker
    speaking about themselves.

*   **Where a fact may appear.** A preferred name was chosen to be addressed by
    and may be used anywhere. An email address is shown only in a direct
    message, and -- because reads are keyed on the viewer -- only to its owner.
    `visible_facts` applies that rule once, so a channel reply cannot show an
    email by a caller forgetting to filter. It returns the same shape whether
    an email is stored or not, so a channel reply cannot confirm one exists.

Facts are DATA. Nothing here renders them into a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog

from chatmemory.domain.identity import Viewer
from chatmemory.ports.facts import (
    FactKind,
    FactRejection,
    FactStore,
    InvalidFact,
    PersonalFact,
    PersonalFacts,
)
from chatmemory.ports.memory import ConversationLocation

log = structlog.get_logger()

# Only ever shown where the one reader is the owner.
DIRECT_ONLY_KINDS = frozenset({
    FactKind.EMAIL,
    FactKind.PHONE,
    FactKind.ETH_WALLET,
    FactKind.BTC_WALLET,
})
"""Every way of reaching a person, and every address that ties them to money.

A wallet is public on its chain; what is private is that it is *theirs*, and a
channel reply naming it makes that link for everyone present. Same reasoning as
the email it joins."""


class FactOutcome(StrEnum):
    STORED = "stored"
    REJECTED = "rejected"
    # Opted out: the trigger dropped the row. Not an error, and not confirmed
    # back as stored.
    NOT_STORED = "not_stored"


@dataclass(frozen=True, slots=True)
class FactResult:
    """What happened to one attempt to set a fact.

    `fact` is the normalised value when stored -- what the confirmation should
    repeat back, so the person sees exactly what was kept rather than what they
    typed. `rejection` says why when it was refused.
    """

    outcome: FactOutcome
    kind: FactKind
    fact: PersonalFact | None = None
    rejection: FactRejection | None = None


def visible_facts(facts: PersonalFacts, location: ConversationLocation) -> PersonalFacts:
    """The facts that may be shown at `location`, which is where the owner asked.

    Outside a direct message, direct-only kinds are dropped. The result carries
    no marker of what was dropped: "not shown here" and "not stored" must be
    indistinguishable in a channel.
    """
    if location.direct:
        return facts
    return PersonalFacts(
        tuple(s for s in facts.facts if s.fact.kind not in DIRECT_ONLY_KINDS)
    )


class PersonalFactsService:
    """Sets, shows and deletes the asker's own facts."""

    def __init__(self, store: FactStore) -> None:
        self._store = store

    async def remember(self, asker: Viewer, kind: FactKind, value: str) -> FactResult:
        """Validate and store one of the asker's own facts.

        Validation happens here as well as in `PersonalFact`, only so that a
        refusal comes back as a result the reply can explain rather than as an
        exception. The value itself is never logged: it may be an email address.
        """
        try:
            fact = PersonalFact(kind, value)
        except InvalidFact as refused:
            log.info(
                "facts.rejected",
                person=str(asker.person),
                kind=refused.kind.value,
                reason=refused.reason.value,
            )
            return FactResult(FactOutcome.REJECTED, refused.kind, rejection=refused.reason)
        stored = await self._store.set_fact(asker.person, fact)
        log.info(
            "facts.set" if stored else "facts.not_stored",
            person=str(asker.person),
            kind=fact.kind.value,
        )
        if not stored:
            return FactResult(FactOutcome.NOT_STORED, fact.kind)
        return FactResult(FactOutcome.STORED, fact.kind, fact=fact)

    async def facts_for(self, asker: Viewer, location: ConversationLocation) -> PersonalFacts:
        """The asker's own facts, as they may be shown where they asked."""
        return visible_facts(await self._store.facts_of(asker), location)

    async def forget(self, asker: Viewer, kind: FactKind) -> bool:
        forgotten = await self._store.forget_fact(asker.person, FactKind(kind))
        log.info("facts.forgotten", person=str(asker.person), kind=kind, existed=forgotten)
        return forgotten

    async def forget_all(self, asker: Viewer) -> int:
        count = await self._store.forget_all_facts(asker.person)
        log.info("facts.forgotten_all", person=str(asker.person), count=count)
        return count
