"""In-memory `UsageSource` and `UsageDirectory` for the usage tests."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from chatmemory.ports.usage import (
    QuestionPage,
    TracedQuestion,
    UsageExclusions,
    UsageRows,
    UsageUnavailable,
    UsageWindow,
    ViewedPerson,
    VoiceUsage,
)


@dataclass
class FakeUsageSource:
    """Rows and questions as the trace store would hold them, before any exclusion.

    `questions_held` holds every traced question per platform id; `count`
    counts them in a range, less the excluded trace ids, as the store's own
    query would.
    """

    rows: UsageRows = field(default_factory=UsageRows)
    questions_held: dict[str, list[TracedQuestion]] = field(default_factory=dict)
    down: bool = False
    #: A store that ignores the `since` bound of `questions`, as a loosened
    #: query would: what the service filters itself must still hold.
    ignores_since: bool = False
    aggregate_calls: list[tuple[UsageWindow, frozenset[str]]] = field(default_factory=list)
    question_calls: list[tuple[str, datetime, datetime, int]] = field(default_factory=list)

    def _check(self) -> None:
        if self.down:
            raise UsageUnavailable("down")

    async def aggregate(
        self, window: UsageWindow, excluded_trace_ids: Collection[str]
    ) -> UsageRows:
        self._check()
        self.aggregate_calls.append((window, frozenset(excluded_trace_ids)))
        return self.rows

    async def count(
        self,
        user_id: str,
        since: datetime,
        until: datetime,
        excluded_trace_ids: Collection[str],
    ) -> int:
        self._check()
        return sum(
            1
            for q in self.questions_held.get(user_id, [])
            if since <= q.timestamp < until and q.trace_id not in excluded_trace_ids
        )

    async def questions(
        self, user_id: str, since: datetime, until: datetime, page: int
    ) -> QuestionPage:
        self._check()
        self.question_calls.append((user_id, since, until, page))
        found = [
            q
            for q in self.questions_held.get(user_id, [])
            if (self.ignores_since or since <= q.timestamp) and q.timestamp < until
        ]
        found.sort(key=lambda q: q.timestamp, reverse=True)
        return QuestionPage(tuple(found), page, 1 if found else 0)


@dataclass
class FakeUsageDirectory:
    exclusion: UsageExclusions = field(default_factory=UsageExclusions)
    display: dict[str, str] = field(default_factory=dict)
    people: dict[str, ViewedPerson] = field(default_factory=dict)
    voice_usage: VoiceUsage = field(default_factory=VoiceUsage)

    async def exclusions(self, window: UsageWindow) -> UsageExclusions:
        return self.exclusion

    async def names(self, user_ids: Sequence[str]) -> Mapping[str, str]:
        return {u: self.display[u] for u in user_ids if u in self.display}

    async def person(self, user_id: str) -> ViewedPerson | None:
        return self.people.get(user_id)

    async def voice(self, window: UsageWindow) -> VoiceUsage:
        return self.voice_usage
