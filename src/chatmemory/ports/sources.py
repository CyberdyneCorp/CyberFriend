"""Platform and model ports."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.messages import Message


class ChatSource(Protocol):
    """A platform we ingest from. Nothing here names Discord."""

    async def backfill(
        self, channel: ChannelRef, before_message_id: int | None, limit: int
    ) -> Sequence[Message]:
        """A page of history older than `before_message_id`, newest first.

        Newest-first so recent history -- which is what people ask about --
        becomes queryable while older history is still importing.
        """
        ...

    def stream(self) -> AsyncIterator[Message]:
        """Messages as they are posted."""
        ...


class EmbeddingClient(Protocol):
    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    @property
    def dimensions(self) -> int: ...
