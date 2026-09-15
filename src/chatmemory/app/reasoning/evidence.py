"""What a run has gathered, and whether the last round added anything.

Deduplication is the mechanism behind no-progress detection. A corrective
round that returns windows the run already holds has not moved, and spending
another attempt on an equivalent query would only cost money to arrive at the
same place. Both forms are caught here: equivalent *queries* are refused
before they are issued, and equivalent *results* end the run after.

This is also where an evidence item's *kind* stops being an internal detail.
Once a run can hold both what colleagues said and what a search engine
returned, a reader who cannot tell the two apart cannot trust either -- so
`source_system` travels from the evidence item into the citation the surface
renders, rather than being dropped at the port boundary. The reasoning loop
is the last place that still knows; after this, only the citation does.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery
from chatmemory.ports.answers import Citation

EXCERPT_CHARS = 240

SOURCE_DISCORD = "discord"
"""The corpus itself: messages people in this server wrote."""

SOURCE_WEB = "web"
"""Anything an egress provider returned. Not the corpus, and never rendered
as though it were: see `app.egress` for why the query that fetched it is
bounded, and `SourcedCitation.from_corpus` for how a reader is told apart."""

CORPUS_SOURCE_SYSTEMS = frozenset({SOURCE_DISCORD})
"""Source systems that *are* the team's own record.

Membership is stated positively rather than as "not the web": a source added
later is outside the corpus until someone deliberately says otherwise, which
is the direction a mistake should fall.
"""


@dataclass(frozen=True, slots=True)
class SourcedCitation(Citation):
    """A citation that still knows what kind of source it came from.

    `Citation` is the port the Discord surface renders and deliberately
    describes a place -- a channel, a message, a link. Adding the kind here
    rather than there keeps the port unchanged for callers that never leave
    the corpus, while making "my colleagues said" and "the internet says"
    distinguishable at the one place a reader actually sees: the reply.
    """

    source_system: str = SOURCE_DISCORD

    @property
    def from_corpus(self) -> bool:
        return self.source_system in CORPUS_SOURCE_SYSTEMS


@dataclass(frozen=True, slots=True)
class Evidence:
    """A retrieved window, plus what a citation needs to point at it.

    `url` and `author_display` come from the retrieval surface rather than
    from the store: the same loop consumes Discord windows and federated
    results, and `source_system` is what lets an answer attribute each.
    """

    window_id: int
    channel: ChannelRef
    text: str
    score: float
    relevance_source: RelevanceSource
    url: str = ""
    author_display: str = ""
    message_ids: tuple[int, ...] = ()
    source_system: str = SOURCE_DISCORD

    @property
    def from_corpus(self) -> bool:
        return self.source_system in CORPUS_SOURCE_SYSTEMS

    @classmethod
    def from_hit(
        cls,
        hit: SearchHit,
        url: str = "",
        author_display: str = "",
        source_system: str = SOURCE_DISCORD,
    ) -> Evidence:
        return cls(
            window_id=hit.window_id,
            channel=hit.channel,
            text=hit.text,
            score=hit.score,
            relevance_source=hit.relevance_source,
            url=url,
            author_display=author_display,
            message_ids=hit.message_ids,
            source_system=source_system,
        )

    def citation(self) -> SourcedCitation:
        return SourcedCitation(
            channel=self.channel,
            # A window cites its first message so the link lands on the
            # conversation; 0 marks a window with no resolvable message.
            message_id=self.message_ids[0] if self.message_ids else 0,
            author_display=self.author_display,
            excerpt=self.text[:EXCERPT_CHARS],
            url=self.url,
            # Carried, not re-derived: whatever produced this item is the
            # only thing that knows where it came from, and a surface that
            # guessed would guess wrong exactly when it matters.
            source_system=self.source_system,
        )


def query_signature(query: SearchQuery) -> tuple[object, ...]:
    """A key under which two retrievals count as the same retrieval.

    Word order and case do not make a query different; the time range, the
    result count and the channel *preference* do.
    """
    terms = tuple(sorted(query.text.lower().split()))
    channels = tuple(sorted(str(c) for c in query.channels)) if query.channels else ()
    authors = tuple(sorted(str(a) for a in query.authors)) if query.authors else ()
    return (terms, query.since, query.until, query.limit, channels, authors)


class EvidenceLedger:
    """The evidence a run holds, keyed by window so re-retrieval is free."""

    def __init__(self) -> None:
        self._items: dict[int, Evidence] = {}
        self._signatures: set[tuple[object, ...]] = set()

    def add(self, items: Iterable[Evidence], source_system: str | None = None) -> int:
        """Add retrieved evidence; return how much of it was new.

        `source_system` stamps items that did not carry their own. Provenance
        is reported per result by the retrieval port but stored per item, and
        without carrying it across that boundary every piece of evidence
        silently re-defaults to the corpus -- so a web result would be
        presented to a reader as something a colleague said.
        """
        new = 0
        for item in items:
            if source_system is not None and item.source_system == SOURCE_DISCORD:
                item = replace(item, source_system=source_system)
            if item.window_id in self._items:
                continue
            self._items[item.window_id] = item
            new += 1
        return new

    def note_query(self, query: SearchQuery) -> bool:
        """Record a query about to be issued; False if it is a repeat."""
        signature = query_signature(query)
        if signature in self._signatures:
            return False
        self._signatures.add(signature)
        return True

    @property
    def items(self) -> tuple[Evidence, ...]:
        return tuple(self._items.values())

    @property
    def window_ids(self) -> frozenset[int]:
        return frozenset(self._items)

    def get(self, window_id: int) -> Evidence | None:
        return self._items.get(window_id)

    def citations(self, window_ids: Sequence[int]) -> tuple[SourcedCitation, ...]:
        """Citations for ids the run actually holds; unknown ids are dropped.

        A model naming a window that was never retrieved is not evidence of
        anything, so it cannot become a citation.
        """
        seen: set[int] = set()
        out: list[SourcedCitation] = []
        for wid in window_ids:
            item = self._items.get(wid)
            if item is None or wid in seen:
                continue
            seen.add(wid)
            out.append(item.citation())
        return tuple(out)

    def __len__(self) -> int:
        return len(self._items)
