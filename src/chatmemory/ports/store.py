"""Persistence and retrieval ports.

Note the asymmetry: writes take whatever they need, but every method that
*returns content* takes a `Viewer` as a required positional argument. An
unfiltered read is therefore not expressible through this interface.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message, Window
from chatmemory.domain.search import SearchHit, SearchQuery


class Store(Protocol):
    """Canonical message storage."""

    async def upsert_messages(self, messages: Sequence[Message]) -> int:
        """Insert or update by (platform message id, revision). Idempotent."""
        ...

    async def tombstone_message(self, platform_message_id: int, at: datetime) -> None:
        """Mark deleted. Content must stop being returned by every read path."""
        ...

    async def purge_channel(self, channel: ChannelRef) -> int:
        """Remove a channel's content when it leaves indexing scope."""
        ...

    async def resolve_person(self, person: PersonRef, display_name: str) -> int:
        """Return the canonical person id, creating it if unseen."""
        ...

    async def get_cursor(self, channel: ChannelRef) -> int | None:
        """Oldest message id imported so far, for resumable backfill."""
        ...

    async def set_cursor(self, channel: ChannelRef, oldest_message_id: int) -> None: ...

    async def messages_without_window(self, limit: int) -> Sequence[Message]: ...

    async def replace_windows(self, channel: ChannelRef, windows: Sequence[Window]) -> int: ...

    async def windows_missing_embeddings(self, limit: int) -> Sequence[Window]: ...

    async def store_embedding(self, window_id: int, embedding: Sequence[float]) -> None: ...


class SearchBackend(Protocol):
    """Retrieval. Every method requires a viewer; none can be called without one."""

    async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
        """Return hits from channels the viewer may read.

        The viewer's channel set must be applied as a predicate *within* the
        query, not to its results: filtering an approximate index after the
        fact silently under-returns, and does so worst for the people in the
        fewest channels.
        """
        ...

    async def thread_context(
        self, viewer: Viewer, platform_message_id: int, radius: int = 10
    ) -> Sequence[Message]:
        """Messages surrounding one the viewer may read, or empty if they may not."""
        ...

    async def list_channels(self, viewer: Viewer) -> Sequence[ChannelRef]:
        """Indexed channels the viewer may read."""
        ...
