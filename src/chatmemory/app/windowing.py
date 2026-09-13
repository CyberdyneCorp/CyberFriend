"""Grouping messages into retrieval units.

A single Discord message is usually a few words and carries almost no
embedding signal, so similarity is computed over *windows* -- a thread, or a
run of messages uninterrupted by a long silence.

Windows are sized in tokens rather than characters. Character sizing makes a
window's real size depend on the corpus's characters-per-token ratio, which
for Discord -- emoji, code snippets, several languages -- varies enough to
push windows past the embedding model's budget without warning.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import timedelta

from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.messages import Message, Window

TokenCounter = Callable[[str], int]


def approximate_tokens(text: str) -> int:
    """Cheap fallback when no tokenizer is configured.

    Deliberately conservative: over-counting splits a window early, which
    costs a little recall. Under-counting silently truncates at the embedding
    model, which costs content.
    """
    return max(1, len(text) // 3)


def _render(message: Message) -> str:
    return f"{message.author.platform_user_id}: {message.content}"


class WindowBuilder:
    def __init__(
        self,
        max_messages: int = 10,
        max_tokens: int = 512,
        gap: timedelta = timedelta(minutes=15),
        count_tokens: TokenCounter = approximate_tokens,
    ) -> None:
        self._max_messages = max_messages
        self._max_tokens = max_tokens
        self._gap = gap
        self._count = count_tokens

    def build(self, channel: ChannelRef, messages: Iterable[Message]) -> list[Window]:
        """Group a channel's messages into windows, oldest first.

        Threaded messages are grouped by thread rather than interleaved with
        unrelated channel activity, since a thread is already a conversation.
        """
        visible = sorted(
            (m for m in messages if m.is_visible),
            key=lambda m: (m.created_at, m.platform_message_id),
        )
        if not visible:
            return []

        windows: list[Window] = []
        for group in self._split_by_thread(visible):
            windows.extend(self._split_group(channel, group))
        windows.sort(key=lambda w: w.starts_at)
        return windows

    def _split_by_thread(self, messages: Sequence[Message]) -> list[list[Message]]:
        channel_level: list[Message] = []
        threads: dict[int, list[Message]] = {}
        for m in messages:
            if m.thread_id is None:
                channel_level.append(m)
            else:
                threads.setdefault(m.thread_id, []).append(m)
        groups = [channel_level] if channel_level else []
        groups.extend(threads.values())
        return groups

    def _split_group(self, channel: ChannelRef, group: Sequence[Message]) -> list[Window]:
        windows: list[Window] = []
        current: list[Message] = []
        tokens = 0

        for message in group:
            piece = self._count(_render(message))
            gap_exceeded = (
                bool(current)
                and (message.created_at - current[-1].created_at) > self._gap
            )
            full = len(current) >= self._max_messages or (
                bool(current) and tokens + piece > self._max_tokens
            )

            if current and (gap_exceeded or full):
                windows.append(self._make(channel, current))
                current, tokens = [], 0

            current.append(message)
            tokens += piece

        if current:
            windows.append(self._make(channel, current))
        return windows

    def _make(self, channel: ChannelRef, messages: Sequence[Message]) -> Window:
        return Window(
            channel=channel,
            message_ids=tuple(m.platform_message_id for m in messages),
            text="\n".join(_render(m) for m in messages),
            starts_at=messages[0].created_at,
            ends_at=messages[-1].created_at,
            thread_id=messages[0].thread_id,
        )
