"""Usage and cost of traced question runs, read live from the trace store.

Two ports, because the facts come from two places:

*   `UsageSource` is the trace store (Langfuse). It holds what was asked, by
    which platform id, with which feature, model tokens, estimated cost and
    tool calls. Every row it returns carries the asker's platform id and the
    day, so the people who must not appear can be removed before anything is
    summed.
*   `UsageDirectory` is our own database. It says who must not appear
    (`UsageExclusions`), what a platform id is called, whether a person was
    told their questions are recorded, and the voice minutes ledger.

There is no usage table of our own: the admin API reads the trace store at
request time, behind this port, so a rollup could be added later without the
console noticing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol


class UsageUnavailable(Exception):
    """The trace store could not be read. Never answered with stale or partial counts."""


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """`[start, end)`, both at midnight UTC: whole days, so a cache key is stable."""

    start: datetime
    end: datetime

    @property
    def days(self) -> int:
        return (self.end - self.start).days


# --- what the trace store returns ----------------------------------------


@dataclass(frozen=True, slots=True)
class TraceCount:
    """Traced question runs of one asker, one day, one feature."""

    user_id: str
    day: date
    feature: str
    count: int


@dataclass(frozen=True, slots=True)
class ModelCall:
    """Model usage of one asker, one day, one feature, one model."""

    user_id: str
    day: date
    feature: str
    model: str
    input_tokens: int
    output_tokens: int
    cost: float


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Federated tool calls of one asker, one day, one feature, one tool."""

    user_id: str
    day: date
    feature: str
    tool: str
    calls: int


@dataclass(frozen=True, slots=True)
class UsageRows:
    """Everything a summary is built from, before anybody is excluded."""

    traces: tuple[TraceCount, ...] = ()
    models: tuple[ModelCall, ...] = ()
    tools: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class TracedQuestion:
    """One question as the console may show it: the asker's words and metadata.

    Nothing else from the trace: no answer, no evidence, no decision trail.
    """

    trace_id: str
    timestamp: datetime
    feature: str
    tools: tuple[str, ...]
    question: str
    input_tokens: int
    output_tokens: int
    cost: float | None


@dataclass(frozen=True, slots=True)
class QuestionPage:
    questions: tuple[TracedQuestion, ...]
    page: int
    total_pages: int


class UsageSource(Protocol):
    """The trace store, scoped to this application's tag and environment.

    Every method raises `UsageUnavailable` when the store cannot be read.
    """

    async def aggregate(
        self, window: UsageWindow, excluded_trace_ids: Collection[str]
    ) -> UsageRows:
        """Per asker and day: runs, model usage and tool calls in `window`.

        Traces in `excluded_trace_ids` are left out by the store's own query.
        """
        ...

    async def count(
        self,
        user_id: str,
        since: datetime,
        until: datetime,
        excluded_trace_ids: Collection[str],
    ) -> int:
        """How many runs `user_id` asked in `[since, until)`."""
        ...

    async def questions(
        self, user_id: str, since: datetime, until: datetime, page: int
    ) -> QuestionPage:
        """One page of `user_id`'s traced questions in `[since, until)`, newest first."""
        ...


# --- what our database says ----------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageExclusions:
    """Who and what must not appear, read from our database for one request.

    `people` (opted out, or an erasure under way) are removed entirely.
    `erased_before` removes a person's runs up to the time of a completed
    erasure. `trace_ids` are traces whose deletion was requested, which the
    trace store may still hold.
    """

    people: frozenset[str] = frozenset()
    erased_before: Mapping[str, datetime] = field(default_factory=dict)
    trace_ids: frozenset[str] = frozenset()

    def excludes_person(self, user_id: str) -> bool:
        return user_id in self.people

    def excludes_day(self, user_id: str, day: date) -> bool:
        """Day granularity, for counts: the day of the erasure goes too."""
        if user_id in self.people:
            return True
        cut = self.erased_before.get(user_id)
        return cut is not None and day <= cut.date()

    def excludes_moment(self, user_id: str, moment: datetime) -> bool:
        """Exact, for question text."""
        if user_id in self.people:
            return True
        cut = self.erased_before.get(user_id)
        return cut is not None and moment <= cut


@dataclass(frozen=True, slots=True)
class ViewedPerson:
    """The person behind a platform id, as the questions view needs them."""

    person_id: int
    display_name: str
    #: When they were told their questions are recorded; None if never.
    notice_at: datetime | None


@dataclass(frozen=True, slots=True)
class VoiceUsage:
    """Transcribed seconds in the months a window touches."""

    #: Per platform id of a person who is not excluded.
    by_user: Mapping[str, int] = field(default_factory=dict)
    #: Seconds folded out of erased people's rows: a server total only.
    anonymous: int = 0


class UsageDirectory(Protocol):
    async def exclusions(self, window: UsageWindow) -> UsageExclusions:
        """Read on every request, so an opt-out or erasure applies at once."""
        ...

    async def names(self, user_ids: Sequence[str]) -> Mapping[str, str]:
        """Current display names by platform id; ids without a name are absent."""
        ...

    async def person(self, user_id: str) -> ViewedPerson | None:
        ...

    async def voice(self, window: UsageWindow) -> VoiceUsage:
        ...
