"""Short-lived conversational context for follow-up questions.

Context is keyed by where the conversation is happening, but the *scope* of
each answer is keyed by who asked. A follow-up from a lower-access person in
the same thread must be answered under their access, never the original
asker's -- so nothing here carries a viewer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Conversation:
    turns: list[str] = field(default_factory=list)
    last_active: float = 0.0


class ConversationStore:
    def __init__(self, idle_timeout_seconds: float = 1800.0, max_turns: int = 6) -> None:
        self._timeout = idle_timeout_seconds
        self._max_turns = max_turns
        self._by_location: dict[int, _Conversation] = {}

    def history(self, location_id: int, now: float | None = None) -> tuple[str, ...]:
        current = time.monotonic() if now is None else now
        convo = self._by_location.get(location_id)
        if convo is None:
            return ()
        if current - convo.last_active > self._timeout:
            # Expired: a later message is a new question, not a follow-up.
            del self._by_location[location_id]
            return ()
        return tuple(convo.turns)

    def record(self, location_id: int, text: str, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        convo = self._by_location.setdefault(location_id, _Conversation())
        convo.turns.append(text)
        del convo.turns[: max(0, len(convo.turns) - self._max_turns)]
        convo.last_active = current

    def clear(self, location_id: int) -> None:
        self._by_location.pop(location_id, None)
