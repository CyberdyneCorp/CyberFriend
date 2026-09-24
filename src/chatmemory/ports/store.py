"""Persistence and retrieval ports.

Note the asymmetry: writes take whatever they need, but every method on
`SearchBackend` takes a `Viewer` as a required positional argument. An
unfiltered *retrieval* is therefore not expressible through this interface.

`Store` carries one carve-out, and it is narrower than it looks.
`messages_without_window`, `windows_missing_embeddings` and
`messages_pending_extraction` return content and take no viewer, because they
exist for windowing, embedding and ask extraction -- work the ingestion
process does on behalf of nobody. There is no viewer to bind, and binding one
would be wrong rather than merely awkward: windowing only the channels some
person may read would leave the rest of the corpus permanently unwindowed, and
therefore permanently unretrievable by anyone.

What makes that safe is not the comment, it is the reachability. Both are
called only from the ingestion entrypoint, which serves no requests and has no
caller to widen; no request-scoped surface -- the bot, the MCP server, the
reasoning loop -- holds a `Store` at all. Adding a content-returning method
here without a viewer means asserting that same property again, and the SQL
audit in tests/unit/test_sql_audit.py requires the reason in writing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import DirtyChannel, Message, Window
from chatmemory.domain.search import PersonCandidate, SearchHit, SearchQuery


@dataclass(frozen=True, slots=True)
class PendingExtraction:
    """A message awaiting ask extraction, and the revision that was read.

    The generation is `DirtyChannel`'s, per message rather than per channel.
    Extraction costs a model call, so the mark has to say *which* revision was
    read: an edit landing while the model was being asked bumps the message's
    revision, and recording the one that was read then leaves the message
    pending instead of clearing work that was never done.

    None means the caller never read a row -- the live pass is handed a
    message by capture -- and the store records whatever revision is current.
    That is exactly as strong as the behaviour it replaces, where a live
    message was extracted once and never revisited.
    """

    message: Message
    generation: int | None = None


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

    # --- ask extraction ---------------------------------------------------
    #
    # The same carve-out as windowing, and the same reason it is safe: these
    # run for the extraction pass, which acts for nobody, and are called only
    # from the ingest entrypoint. Extracting only the channels some person may
    # read would leave the rest of a server's obligations permanently
    # unextracted, which is not a narrower feature but a broken one.
    #
    # They are on the port rather than probed for at runtime because history
    # is most of what a channel contains: a process that silently skips them
    # answers "what did people ask me to do?" from whatever happened while it
    # was running, and looks entirely healthy doing it.

    async def messages_pending_extraction(
        self, limit: int, channels: Sequence[ChannelRef] = ()
    ) -> Sequence[PendingExtraction]:
        """Messages whose current revision has not been through extraction.

        Newest first, and the last few minutes are left out: a message
        captured moments ago is probably still buffered in the live pass and
        about to be extracted from there, so offering it here as well buys the
        same model call twice.
        """
        ...

    async def record_extraction(self, entries: Sequence[PendingExtraction]) -> int:
        """Record that each entry's revision has been extracted.

        Recording a revision an edit has already moved past leaves the message
        pending, so a message edited mid-extraction is re-read rather than
        being marked done with text nobody extracted.
        """
        ...

    async def pending_extraction_count(
        self, cap: int = 1000, channels: Sequence[ChannelRef] = ()
    ) -> int:
        """How much is waiting, counted no further than `cap`.

        Bounded because this is a health figure on a corpus that may hold
        millions of messages, and "is the backlog draining" is answered by
        "1000+" exactly as well as by the true number.
        """
        ...

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

    async def people_named(
        self, viewer: Viewer, name: str, limit: int = 6
    ) -> Sequence[PersonCandidate]:
        """People a typed name may refer to, best match first, at most `limit`.

        Only people with a live message in a channel the viewer may read are
        candidates, applied in the statement: a list of names is itself a
        disclosure, and somebody known only from a private channel must not
        appear in it. An exact full name beats a first-name or prefix match.
        """
        ...
