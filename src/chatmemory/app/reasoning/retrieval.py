"""The corpus, as the one door a run has onto it.

`ports.RetrievalTool` is deliberately narrow, and this is the adapter that
satisfies it from the permission-filtered search backend. It adds nothing to
what that backend returns: the viewer arrives from `scope.retrieval_viewer`
and is handed straight to the store, where the readable-channel set is bound
into the same statement as the ranking. There is no path through this class
that can widen a search, because there is no parameter here that could.

Two conversions happen, and only two. A `SearchHit` becomes `Evidence` with a
link a reader can click, and a store that could not be consulted becomes
`RetrievalUnavailable` rather than an empty result -- reporting that nobody
said anything when in truth we could not look is the one answer that is
always wrong.

`WithheldRetrieval` lives here too, and is deliberately a separate class from
`CorpusRetrieval` rather than a second method on it. It searches a *different*
viewer -- the asker, restricted to what the audience may not read -- and the
one thing that must never happen is for what it sees to be mistaken for
evidence. Keeping it out of the type the reasoning loop holds means the loop
has no way to reach it at all.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from chatmemory.app.reasoning.errors import RetrievalUnavailable
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.ports import RetrievalResult
from chatmemory.domain.audience import Audience
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.domain.search import SearchHit, SearchQuery
from chatmemory.ports.store import SearchBackend

log = structlog.get_logger()

SOURCE_SYSTEM = "discord"

GAP_PROBE_LIMIT = 5
"""How deep the withheld-evidence probe looks.

It only has to answer "is there anything here", and it names channels rather
than ranking them, so a handful of hits settles the question. The probe is an
extra query on every public question from someone with private access; making
it cheap is what keeps that acceptable.
"""

EvidenceUrl = Callable[[ChannelRef, int | None], str]
"""Where a window points. Injected so the core never hardcodes a platform."""


def discord_urls(guild_id: int) -> EvidenceUrl:
    """Deep links, so a citation resolves in one click.

    A window whose message ids were not resolved links to the channel: the
    conversation is a worse destination than the exact message, and a much
    better one than a link to message zero.
    """

    def build(channel: ChannelRef, message_id: int | None) -> str:
        base = f"https://discord.com/channels/{guild_id}/{channel.platform_channel_id}"
        return base if message_id is None else f"{base}/{message_id}"

    return build


class CorpusRetrieval:
    """`RetrievalTool` over the viewer-scoped search backend."""

    def __init__(self, search: SearchBackend, url: EvidenceUrl) -> None:
        self._search = search
        self._url = url

    async def retrieve(self, viewer: Viewer, query: SearchQuery) -> RetrievalResult:
        try:
            hits = await self._search.search(viewer, query)
        except Exception as exc:
            # Whatever failed -- the database, the embedding endpoint, the
            # network -- the run must report a dependency failure. The driver
            # turns this into a failed run; an empty result would instead be
            # rendered to the asker as "I found nothing".
            raise RetrievalUnavailable(
                f"the corpus could not be searched: {type(exc).__name__}"
            ) from exc
        return RetrievalResult(
            items=tuple(self._evidence(hit) for hit in hits),
            # Not derivable here. The signal means "relevant content exists
            # that this viewer may not read", which only the permission
            # predicate itself can count; the backend does not report it yet,
            # and inferring it from an empty result would be a guess that
            # operators would read as fact.
            access_blocked=False,
            source_system=SOURCE_SYSTEM,
        )

    def _evidence(self, hit: SearchHit) -> Evidence:
        # A window cites its opening message, so the link lands where the
        # conversation starts rather than in the middle of it. The store
        # resolves these ids in the same statement as the search; a hit that
        # arrives without them still gets a working link, to the channel.
        message_id = hit.message_ids[0] if hit.message_ids else None
        return Evidence.from_hit(hit, url=self._url(hit.channel, message_id))


class WithheldRetrieval:
    """What scoping an answer to its audience cost the asker.

    Satisfies `disclosure.WithheldEvidenceProbe`. It searches the gap --
    channels the asker may read and the audience may not -- as the asker, and
    returns the channels that had something to say. That is the whole output:
    no text, no excerpt, no `Evidence`. The answer this informs goes to the
    audience, so anything richer would be one bad assignment away from being
    published to exactly the people the gap exists to exclude.

    Grounding it in a search rather than in set arithmetic is the difference
    between a useful notice and a reflex. Every public question from someone
    with any private channel at all satisfies "the gap is non-empty"; only a
    search can say whether a fuller answer actually exists to go and get.
    """

    def __init__(self, search: SearchBackend, limit: int = GAP_PROBE_LIMIT) -> None:
        self._search = search
        self._limit = limit

    async def withheld_channels(
        self, asker: Viewer, audience: Audience, text: str
    ) -> frozenset[ChannelRef]:
        gap = asker.visible_channels - audience.readable_channels
        if not gap:
            return frozenset()

        # A viewer strictly narrower than the asker's own access: the probe
        # can only ever look at channels this person may already read, so it
        # cannot become a way to widen anyone's view.
        probe = Viewer(person=asker.person, visible_channels=gap)
        try:
            hits = await self._search.search(probe, SearchQuery(text=text, limit=self._limit))
        except Exception as exc:
            # The answer is the product; the notice is a courtesy. A probe
            # that fails costs the asker a "there is more in your DMs" hint,
            # and must never cost anyone the answer they asked for.
            log.warning("withheld.probe_failed", error=type(exc).__name__)
            return frozenset()

        # Intersected with the gap rather than trusted: a backend that ignored
        # its viewer would otherwise be able to name channels in the notice.
        return frozenset(hit.channel for hit in hits) & gap
