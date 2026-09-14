"""What a run has gathered, and whether the last round added anything.

Deduplication is the mechanism behind no-progress detection. A corrective
round that returns windows the run already holds has not moved, and spending
another attempt on an equivalent query would only cost money to arrive at the
same place. Both forms are caught here: equivalent *queries* are refused
before they are issued, and equivalent *results* end the run after.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery
from chatmemory.ports.answers import Citation

EXCERPT_CHARS = 240


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
    source_system: str = "discord"

    @classmethod
    def from_hit(cls, hit: SearchHit, url: str = "", author_display: str = "") -> Evidence:
        return cls(
            window_id=hit.window_id,
            channel=hit.channel,
            text=hit.text,
            score=hit.score,
            relevance_source=hit.relevance_source,
            url=url,
            author_display=author_display,
            message_ids=hit.message_ids,
        )

    def citation(self) -> Citation:
        return Citation(
            channel=self.channel,
            # A window cites its first message so the link lands on the
            # conversation; 0 marks a window with no resolvable message.
            message_id=self.message_ids[0] if self.message_ids else 0,
            author_display=self.author_display,
            excerpt=self.text[:EXCERPT_CHARS],
            url=self.url,
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

    def add(self, items: Iterable[Evidence]) -> int:
        """Add retrieved evidence; return how much of it was new."""
        new = 0
        for item in items:
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

    def citations(self, window_ids: Sequence[int]) -> tuple[Citation, ...]:
        """Citations for ids the run actually holds; unknown ids are dropped.

        A model naming a window that was never retrieved is not evidence of
        anything, so it cannot become a citation.
        """
        seen: set[int] = set()
        out: list[Citation] = []
        for wid in window_ids:
            item = self._items.get(wid)
            if item is None or wid in seen:
                continue
            seen.add(wid)
            out.append(item.citation())
        return tuple(out)

    def __len__(self) -> int:
        return len(self._items)
