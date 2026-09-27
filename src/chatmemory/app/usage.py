"""Usage and cost per person and feature, read live, with exclusions applied every time.

The console's usage view is built here from two ports: the trace store
(`UsageSource`, Langfuse) and our database (`UsageDirectory`). Three rules
carry the weight:

*   **Exclusion is never cached.** The trace store's rows are cached for five
    minutes, keyed by window and by the traces whose deletion was requested
    (the store's own query leaves those out, so a new deletion request is a
    new key). Who has opted out, is being erased or has erased is read from
    our database on every request and applied to the cached rows, so an
    opt-out takes effect on the next request rather than when the cache
    expires.
*   **Question text is never cached**, and only questions traced after the
    person was told their questions are recorded are returned as text.
    Earlier ones are counted, not shown.
*   **A store that cannot be read is said to be unreadable.** `UsageUnavailable`
    propagates; nothing stale is shown as current.

Totals are "traced question runs": background work and opted-out askers are
never traced, so this is an undercount by design, and labelled as one.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from chatmemory.ports.usage import (
    ModelCall,
    ToolCall,
    TraceCount,
    TracedQuestion,
    UsageDirectory,
    UsageExclusions,
    UsageRows,
    UsageSource,
    UsageWindow,
    ViewedPerson,
    VoiceUsage,
)

MAX_WINDOW_DAYS = 90
DEFAULT_WINDOW_DAYS = 30
CACHE_SECONDS = 300.0
CACHE_ENTRIES = 32
TOTALS_LABEL = "traced question runs"
MAX_PAGE = 1000
"""50 questions a page: far beyond a 90-day window of anybody's questions."""
BIGINT_MAX = 2**63 - 1
_DIGITS = re.compile(r"[0-9]{1,19}")
VOICE_FEATURE = "voice"
"""Voice minutes are shown as a feature of their own; they are not traced runs."""


_INSTANT = timedelta(microseconds=1)


class Grouping(StrEnum):
    PERSON = "person"
    FEATURE = "feature"
    MODEL = "model"
    TOOL = "tool"


class BadWindow(ValueError):
    """A window the console will not read: malformed, reversed or too long."""


class BadPage(ValueError):
    """A page number the console will not read: not 1..MAX_PAGE in ASCII digits."""


def window_of(start: str | None, end: str | None, today: date) -> UsageWindow:
    """`from` and `to` as inclusive ISO dates; the last 30 days by default."""
    last = _day(end, "to") if end else today
    first = _day(start, "from") if start else last - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    if first > last:
        raise BadWindow("from must not be after to")
    if (last - first).days + 1 > MAX_WINDOW_DAYS:
        raise BadWindow(f"the window is at most {MAX_WINDOW_DAYS} days")
    return UsageWindow(_midnight(first), _midnight(last + timedelta(days=1)))


def _day(raw: str, name: str) -> date:
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError as exc:
        raise BadWindow(f"{name} must be a date (YYYY-MM-DD)") from exc


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


# --- the summary -----------------------------------------------------------


@dataclass(slots=True)
class UsageLine:
    """One row of the summary. A figure that does not apply to the grouping is None."""

    key: str
    name: str | None = None
    questions: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    tool_calls: int | None = None
    tools: set[str] = field(default_factory=set)
    voice_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class UsageTotals:
    questions: int
    input_tokens: int
    output_tokens: int
    cost: float
    tool_calls: int
    voice_seconds: int


@dataclass(frozen=True, slots=True)
class UsageSummary:
    window: UsageWindow
    grouping: Grouping
    lines: tuple[UsageLine, ...]
    totals: UsageTotals
    #: How long a trace is kept (`TRACE_RETENTION_DAYS`), for the screen to
    #: state; None when this process could not read it.
    retention_days: int | None
    label: str = TOTALS_LABEL


@dataclass(frozen=True, slots=True)
class QuestionsView:
    user_id: str
    person: ViewedPerson | None
    questions: tuple[TracedQuestion, ...]
    page: int
    total_pages: int
    #: Runs traced before the person was told, in the window: counted, never shown.
    hidden_before_notice: int


class _Cache:
    """Pre-exclusion rows per key, for at most `ttl` seconds."""

    def __init__(self, ttl: float, now: Callable[[], float]) -> None:
        self._ttl = ttl
        self._now = now
        self._entries: dict[object, tuple[float, UsageRows]] = {}

    def get(self, key: object) -> UsageRows | None:
        entry = self._entries.get(key)
        if entry is None or self._now() - entry[0] >= self._ttl:
            return None
        return entry[1]

    def put(self, key: object, rows: UsageRows) -> None:
        now = self._now()
        self._entries = {
            k: v for k, v in self._entries.items() if now - v[0] < self._ttl
        }
        while len(self._entries) >= CACHE_ENTRIES:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = (now, rows)


class UsageService:
    def __init__(
        self,
        source: UsageSource,
        directory: UsageDirectory,
        *,
        retention_days: int | None,
        ttl: float = CACHE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._directory = directory
        self._retention_days = retention_days
        self._cache = _Cache(ttl, monotonic)

    async def summary(self, window: UsageWindow, grouping: Grouping) -> UsageSummary:
        exclusions = await self._directory.exclusions(window)
        rows = _kept(await self._rows(window, exclusions.trace_ids), exclusions)
        voice = _kept_voice(await self._directory.voice(window), exclusions)
        lines = _group(rows, voice, grouping)
        if grouping is Grouping.PERSON:
            await self._name(lines)
        return UsageSummary(
            window, grouping, tuple(lines), _totals(rows, voice), self._retention_days
        )

    async def person(self, user_id: str) -> ViewedPerson | None:
        return await self._directory.person(user_id)

    async def questions(
        self,
        user_id: str,
        person: ViewedPerson | None,
        window: UsageWindow,
        page: int,
    ) -> QuestionsView:
        """`user_id`'s questions after their notice; earlier ones only counted."""
        exclusions = await self._directory.exclusions(window)
        if exclusions.excludes_person(user_id):
            return QuestionsView(user_id, person, (), page, 0, 0)
        erased = exclusions.erased_before.get(user_id)
        # The moment of the erasure is itself erased (`excludes_moment`).
        floor = max(window.start, erased + _INSTANT) if erased else window.start
        notice = person.notice_at if person else None
        hidden_until = window.end if notice is None else min(max(notice, floor), window.end)
        hidden = await self._hidden(user_id, floor, hidden_until, exclusions)
        if notice is None or max(floor, notice) >= window.end:
            return QuestionsView(user_id, person, (), page, 0, hidden)
        found = await self._source.questions(user_id, max(floor, notice), window.end, page)
        shown = tuple(
            q
            for q in found.questions
            if q.trace_id not in exclusions.trace_ids
            and not exclusions.excludes_moment(user_id, q.timestamp)
            and q.timestamp >= notice
        )
        return QuestionsView(user_id, person, shown, found.page, found.total_pages, hidden)

    async def _hidden(
        self, user_id: str, since: datetime, until: datetime, exclusions: UsageExclusions
    ) -> int:
        if since >= until:
            return 0
        return await self._source.count(user_id, since, until, exclusions.trace_ids)

    async def _rows(self, window: UsageWindow, trace_ids: frozenset[str]) -> UsageRows:
        key = (window.start, window.end, trace_ids)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        rows = await self._source.aggregate(window, trace_ids)
        self._cache.put(key, rows)
        return rows

    async def _name(self, lines: list[UsageLine]) -> None:
        names = await self._directory.names([line.key for line in lines])
        for line in lines:
            line.name = names.get(line.key) or None


def _kept(rows: UsageRows, exclusions: UsageExclusions) -> UsageRows:
    return UsageRows(
        traces=tuple(r for r in rows.traces if not exclusions.excludes_day(r.user_id, r.day)),
        models=tuple(r for r in rows.models if not exclusions.excludes_day(r.user_id, r.day)),
        tools=tuple(r for r in rows.tools if not exclusions.excludes_day(r.user_id, r.day)),
    )


def _kept_voice(voice: VoiceUsage, exclusions: UsageExclusions) -> VoiceUsage:
    """The directory already leaves excluded people out; checked again here."""
    by_user = {u: s for u, s in voice.by_user.items() if not exclusions.excludes_person(u)}
    return VoiceUsage(by_user=by_user, anonymous=voice.anonymous)


def _group(rows: UsageRows, voice: VoiceUsage, grouping: Grouping) -> list[UsageLine]:
    lines: dict[str, UsageLine] = {}
    if grouping in (Grouping.PERSON, Grouping.FEATURE):
        by_person = grouping is Grouping.PERSON
        _add_runs(lines, rows.traces, lambda r: r.user_id if by_person else r.feature)
        _add_models(lines, rows.models, lambda r: r.user_id if by_person else r.feature)
        _add_tools(lines, rows.tools, lambda r: r.user_id if by_person else r.feature)
        _add_voice(lines, voice, by_person)
    elif grouping is Grouping.MODEL:
        _add_models(lines, rows.models, lambda r: r.model)
    else:
        _add_tools(lines, rows.tools, lambda r: r.tool)
    return sorted(lines.values(), key=lambda line: (-(line.cost or 0.0), line.key))


def _line(lines: dict[str, UsageLine], key: str) -> UsageLine:
    return lines.setdefault(key, UsageLine(key))


def _add_runs(
    lines: dict[str, UsageLine], rows: Iterable[TraceCount], key: Callable[[TraceCount], str]
) -> None:
    for row in rows:
        line = _line(lines, key(row))
        line.questions = (line.questions or 0) + row.count


def _add_models(
    lines: dict[str, UsageLine], rows: Iterable[ModelCall], key: Callable[[ModelCall], str]
) -> None:
    for row in rows:
        line = _line(lines, key(row))
        line.input_tokens = (line.input_tokens or 0) + row.input_tokens
        line.output_tokens = (line.output_tokens or 0) + row.output_tokens
        line.cost = (line.cost or 0.0) + row.cost


def _add_tools(
    lines: dict[str, UsageLine], rows: Iterable[ToolCall], key: Callable[[ToolCall], str]
) -> None:
    for row in rows:
        line = _line(lines, key(row))
        line.tool_calls = (line.tool_calls or 0) + row.calls
        line.tools.add(row.tool)


def _add_voice(lines: dict[str, UsageLine], voice: VoiceUsage, by_person: bool) -> None:
    if by_person:
        for user_id, seconds in voice.by_user.items():
            _line(lines, user_id).voice_seconds = seconds
        return
    total = sum(voice.by_user.values()) + voice.anonymous
    if total:
        _line(lines, VOICE_FEATURE).voice_seconds = total


def _totals(rows: UsageRows, voice: VoiceUsage) -> UsageTotals:
    return UsageTotals(
        questions=sum(r.count for r in rows.traces),
        input_tokens=sum(r.input_tokens for r in rows.models),
        output_tokens=sum(r.output_tokens for r in rows.models),
        cost=sum(r.cost for r in rows.models),
        tool_calls=sum(r.calls for r in rows.tools),
        voice_seconds=sum(voice.by_user.values()) + voice.anonymous,
    )


def platform_id(raw: str) -> str | None:
    """`raw` if it is a platform account id (ASCII digits that fit a bigint), else None.

    `str.isdigit` is not enough: it accepts '²' and other non-ASCII digits,
    which `int()` then refuses, and any length.
    """
    if not _DIGITS.fullmatch(raw) or int(raw) > BIGINT_MAX:
        return None
    return raw


def page_of(raw: str | None) -> int:
    """A 1-based page number, page 1 when absent; anything malformed is `BadPage`."""
    if raw is None or raw == "":
        return 1
    if not _DIGITS.fullmatch(raw) or not 1 <= int(raw) <= MAX_PAGE:
        raise BadPage(f"page must be a whole number from 1 to {MAX_PAGE}")
    return int(raw)

