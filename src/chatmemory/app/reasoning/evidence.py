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

EXTERNAL_CHANNEL_ID = 0
"""The channel id an item that belongs to no channel is filed under.

Paired with the source system as its platform, so an external item's
`ChannelRef` reads `issues:0` and can never compare equal to a Discord
channel. Nothing decides a permission from it -- `Citation.is_corpus` is
false for these, so the delivery guard never tests the channel at all -- but
a placeholder that *could* collide with a real channel would put that
guarantee one refactor away from being wrong.
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
        self._external = 0

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

    def add_external(
        self,
        text: str,
        source_system: str,
        attribution: str = "",
        url: str = "",
    ) -> Evidence:
        """Fold in one result from a system that is not the corpus.

        Not `add`: a retrieved window arrives with an id the store gave it,
        while a tool result has no window anywhere. It still needs one,
        because the window id is what the model is shown inside the fence and
        what it cites back -- an external result without one could be read but
        never attributed, and an answer that rests on it would look like an
        answer resting on nothing.

        The minted ids are negative. A corpus window id is a database row id
        and therefore positive, so an external item can never take the place
        of a retrieved one in this ledger, and a model that cites `-1` cannot
        have meant somebody's message.

        `source_system` is required and travels with the item into the
        citation: this is the one moment where "the internet said" and "a
        colleague said" could still be conflated, and everything downstream
        only reads what is recorded here.
        """
        if not source_system or source_system in CORPUS_SOURCE_SYSTEMS:
            raise ValueError(
                f"external evidence needs a source system outside the corpus, got "
                f"{source_system!r}"
            )
        self._external += 1
        item = Evidence(
            window_id=-self._external,
            channel=ChannelRef(source_system, EXTERNAL_CHANNEL_ID),
            text=text,
            # There is no retrieval score: this was not ranked against
            # anything. Zero, from the least committal source, so that
            # nothing downstream can read it as a relevance judgement -- the
            # gate only ever thresholds a `RERANKED` score, which this is not.
            score=0.0,
            relevance_source=RelevanceSource.LEXICAL,
            url=url,
            author_display=attribution,
            source_system=source_system,
        )
        self._items[item.window_id] = item
        return item

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

    @property
    def external(self) -> tuple[Evidence, ...]:
        """What this run holds that did not come from the corpus.

        A run whose corpus search found nothing may still hold an external
        result, and those two facts have to be separable: "nothing was found"
        is about the corpus, and answering it over evidence the run is holding
        would be false.
        """
        return tuple(item for item in self._items.values() if not item.from_corpus)

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
