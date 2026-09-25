"""Per-person opt-out: withdrawing from the corpus, and staying withdrawn.

Three things have to hold for this to be an opt-out rather than a gesture.

*   **It covers everything the person put in.** Messages, the windows built
    over them, the asks extracted from them, the reactions they left -- and the
    documents they uploaded. An opt-out that covers messages but leaves the PDF
    someone attached fully searchable has withdrawn the index entry and kept
    the content, which is the wrong half.

*   **It survives re-ingestion.** Backfill re-reads history from the platform,
    which still has the messages. Enforcing exclusion here, in the service,
    would mean every present and future write path has to remember to call it.
    So the real enforcement is a database trigger (migration 0008): a row whose
    author has opted out is silently dropped before it is stored, whatever
    issued the INSERT. `excludes` and `filter_messages` below are a courtesy to
    the ingestion loop -- they save the round trip, they are not the guarantee.

*   **It covers what they asked the assistant.** Conversation memory is a
    record of the person's own questions and the answers they were given. It
    is purged by a trigger on `person_opt_out` itself (migration 0013), in the
    same transaction as `record_opt_out` -- so the flag and the memory purge
    cannot be separated, and no path that records an exclusion can forget the
    memory half. The same migration drops any turn or summary written for an
    excluded person, as 0008 does for messages.

*   **It covers what they told the assistant about themselves.** Personal
    facts -- preferred name, email, preferred language -- are purged by a
    second trigger on `person_opt_out` (migration 0014), in the same
    transaction as the flag, and a fact written for an excluded person is
    dropped before it is stored. No call here: as with memory, the guarantee
    belongs to the database, so no path that records an opt-out can keep an
    email address.

*   **One delete path for everything derived from the person.** Since
    migration 0028 the per-table purges above, and scheduled tasks and MCP
    tokens, live in one SQL function, `purge_person_derived(person_id)`, which
    the single `person_opt_out` trigger calls and erasure calls directly. A new
    table holding person data adds its DELETE there, in its own migration.
    The fetch log (`document_fetch`) is keyed on message ids, so it goes with
    the message purge below rather than with the function.

*   **The flag lands before the purge.** In the other order there is a window
    between "content deleted" and "exclusion recorded" in which a backfill page
    re-imports exactly what was just removed, and the opt-out reports success.

Opting back in clears the exclusion and restores nothing. Purged content is
gone; only what the platform still holds and a later backfill re-reads comes
back. That asymmetry is deliberate and is stated in docs/operations.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog

from chatmemory.domain.identity import PersonRef
from chatmemory.domain.messages import Message

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class PersonPurge:
    """What was removed from the conversation corpus for one person."""

    windows: int = 0
    messages: int = 0
    asks: int = 0
    reactions: int = 0
    mentions: int = 0
    decisions: int = 0
    fetch_records: int = 0

    @property
    def total(self) -> int:
        return (
            self.windows
            + self.messages
            + self.asks
            + self.reactions
            + self.mentions
            + self.decisions
            + self.fetch_records
        )


@dataclass(frozen=True, slots=True)
class OptOutReport:
    person: PersonRef
    corpus: PersonPurge = PersonPurge()
    documents: int = 0

    @property
    def total(self) -> int:
        return self.corpus.total + self.documents


class OptOutRegistry(Protocol):
    """Storage for the exclusion list and the purge that goes with it."""

    async def record_opt_out(self, person: PersonRef, reason: str = "") -> None:
        """Record the exclusion. Idempotent; re-recording updates the reason."""
        ...

    async def clear_opt_out(self, person: PersonRef) -> None:
        """Stop excluding this person. Restores nothing that was purged."""
        ...

    async def is_opted_out(self, person: PersonRef) -> bool: ...

    async def purge_person(self, person: PersonRef) -> PersonPurge:
        """Remove everything this person contributed to the conversation corpus.

        Includes the windows their messages appear in, in full: a window is one
        block of text, so the only way to remove their words from it is to
        remove it and let the surviving neighbours be re-formed without them.
        """
        ...


class PersonDocumentPurge(Protocol):
    """The document corpus's half of an opt-out. Satisfied by the document store."""

    async def purge_person_documents(self, person: PersonRef) -> int: ...


class OptOutService:
    def __init__(
        self,
        registry: OptOutRegistry,
        documents: PersonDocumentPurge | None = None,
    ) -> None:
        self._registry = registry
        self._documents = documents

    async def opt_out(self, person: PersonRef, reason: str = "") -> OptOutReport:
        # Flag first, purge second. The reverse order leaves a window between
        # the delete and the flag in which a backfill page re-imports exactly
        # what was just removed -- and the opt-out still reports success.
        await self._registry.record_opt_out(person, reason)
        corpus = await self._registry.purge_person(person)

        documents = 0
        if self._documents is None:
            # Loud, because "messages only" is not an opt-out and an operator
            # reading a success line has no other way to find that out.
            log.error(
                "optout.documents_not_covered",
                person=str(person),
                hint="no document store wired; uploads by this person remain indexed",
            )
        else:
            documents = await self._documents.purge_person_documents(person)

        report = OptOutReport(person=person, corpus=corpus, documents=documents)
        log.info(
            "optout.recorded",
            person=str(person),
            windows=corpus.windows,
            messages=corpus.messages,
            asks=corpus.asks,
            reactions=corpus.reactions,
            mentions=corpus.mentions,
            decisions=corpus.decisions,
            fetch_records=corpus.fetch_records,
            documents=documents,
        )
        return report

    async def opt_in(self, person: PersonRef) -> None:
        """Clear the exclusion. Nothing purged comes back."""
        await self._registry.clear_opt_out(person)
        log.info("optout.cleared", person=str(person))

    async def excludes(self, person: PersonRef) -> bool:
        return await self._registry.is_opted_out(person)

    async def filter_messages(self, messages: Sequence[Message]) -> list[Message]:
        """Drop messages by people who have opted out, before they are stored.

        An optimisation, not the enforcement point: the database rejects these
        rows whatever writes them. Keeping it here as well means a backfill of
        a channel an opted-out person talks in does not spend a round trip per
        message discovering that.
        """
        if not messages:
            return []
        authors = {m.author for m in messages}
        excluded = {a for a in authors if await self._registry.is_opted_out(a)}
        if not excluded:
            return list(messages)
        kept = [m for m in messages if m.author not in excluded]
        log.info("optout.filtered", dropped=len(messages) - len(kept), people=len(excluded))
        return kept
