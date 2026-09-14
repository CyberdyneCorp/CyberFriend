"""Persistence and retrieval ports.

Note the asymmetry: writes take whatever they need, but every method on
`SearchBackend` takes a `Viewer` as a required positional argument. An
unfiltered *retrieval* is therefore not expressible through this interface.

`Store` carries one carve-out, and it is narrower than it looks.
`messages_without_window` and `windows_missing_embeddings` return content and
take no viewer, because they exist for windowing and embedding -- work the
ingestion process does on behalf of nobody. There is no viewer to bind, and
binding one would be wrong rather than merely awkward: windowing only the
channels some person may read would leave the rest of the corpus permanently
unwindowed, and therefore permanently unretrievable by anyone.

What makes that safe is not the comment, it is the reachability. Both are
called only from the ingestion entrypoint, which serves no requests and has no
caller to widen; no request-scoped surface -- the bot, the MCP server, the
reasoning loop -- holds a `Store` at all. Adding a content-returning method
here without a viewer means asserting that same property again, and the SQL
audit in tests/unit/test_sql_audit.py requires the reason in writing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import DirtyChannel, Message, Window
from chatmemory.domain.search import SearchHit, SearchQuery


class Store(Protocol):
    """Canonical message storage."""

    async def upsert_messages(self, messages: Sequence[Message]) -> int:
        """Insert or update by (platform message id, revision). Idempotent."""
        ...

    async def tombstone_message(self, platform_message_id: int, at: datetime) -> None:
        """Mark deleted. Content must stop being returned by every read path.

        Valid for an id the store has never seen: a live message is queued and
        written asynchronously while a deletion goes straight through, so the
        deletion often arrives first. An implementation that can only withdraw
        rows it already holds leaves retracted content live forever.
        """
        ...

    async def stored_revisions(
        self, channel: ChannelRef, since: datetime
    ) -> Mapping[int, datetime]:
        """Message id -> revision, for reconciliation.

        The one method on this port that returns something without a viewer,
        and the reason it may: revisions are timestamps, so this can never
        become a way to read content unfiltered.

        It is part of the port rather than probed for at runtime because
        reconciliation is not optional. A gateway event fired while the
        process was down is never replayed, so a process that silently skips
        reconciliation leaves a message deleted during a deploy retrievable
        indefinitely -- the same class of defect as a process that silently
        does not embed.
        """
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

    async def mark_windows_dirty(self, channel: ChannelRef, at: datetime) -> None:
        """Record that a channel's windows need re-forming from `at` onwards."""
        ...

    async def dirty_channels(self, limit: int = 20) -> Sequence[DirtyChannel]: ...

    async def clear_windows_dirty(self, channel: ChannelRef, generation: int) -> None:
        """Clear the mark only if nothing was marked since `generation` was read."""
        ...

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
