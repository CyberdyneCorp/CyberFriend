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
"""

from __future__ import annotations

from collections.abc import Callable

from chatmemory.app.reasoning.errors import RetrievalUnavailable
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.ports import RetrievalResult
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.domain.search import SearchHit, SearchQuery
from chatmemory.ports.store import SearchBackend

SOURCE_SYSTEM = "discord"

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
        message_id = hit.message_ids[0] if hit.message_ids else None
        return Evidence.from_hit(hit, url=self._url(hit.channel, message_id))
