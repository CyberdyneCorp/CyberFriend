"""Time spans a question names, in Portuguese or English.

`parse_span("o que ela disse ontem?", now, tz)` is the span "ontem" covers,
as half-open UTC bounds `[start, end)`. It is lexical and pure, like the rest
of routing: no model call, no clock of its own. The caller passes "now" from
the process's `Edges.clock` and the zone from `ANSWER_TIMEZONE`.

Days are the deployment's days, not UTC's. "Ontem" asked at 22:00 in Sao
Paulo is already "today" in UTC, and a span cut at UTC midnight would answer
about the wrong day for three hours of every evening.

Weeks are calendar weeks starting on Monday: "semana passada" is the previous
Monday to Monday, not the rolling seven-to-fourteen days ago that
`routing.Period` means by "last week". The two coexist until the routes that
read `Period` move onto this module; that move changes what those routes
answer, so it is its own change.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import NamedTuple


@dataclass(frozen=True, slots=True)
class Span:
    """A span of time a question named.

    `start` and `end` are UTC and half-open. `label` is a stable key for
    provenance and tests ("yesterday", "last_week", "2026-09",
    "since:monday"), not text for a reader: a reply formats the bounds in the
    asker's language itself.
    """

    start: datetime
    end: datetime
    label: str


@dataclass(frozen=True, slots=True)
class _Range:
    """Local calendar days, before they are placed in a timezone."""

    first: date
    #: The first day NOT covered. None runs up to now.
    after: date | None
    label: str


_Resolver = Callable[[re.Match[str], date], _Range | None]


class _Rule(NamedTuple):
    pattern: re.Pattern[str]
    resolve: _Resolver


#: Longer than a year is not a span anybody names in days, and an unbounded
#: number is a date arithmetic overflow waiting for "last 999999999 days".
MAX_DAYS = 366

_WEEKDAYS = {
    "segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4,
    "sabado": 5, "domingo": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
}

_WEEKDAY_LABELS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
    "dezembro": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_NUMBER_WORDS = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5,
    "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10, "quinze": 15,
    "trinta": 30,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "thirty": 30,
}


def _alternation(words: dict[str, int]) -> str:
    # Longest first, so "sexta" is never read as a prefix of a longer word.
    return "|".join(sorted(words, key=len, reverse=True))


_SINCE_WORDS = r"since|desde|a partir d[aeo]"
#: "desde ontem", "since the 21/09", "desde a semana passada": a since-word
#: right before a span opens it up to now.
_SINCE_BEFORE = re.compile(rf"\b(?:{_SINCE_WORDS}) (?:(?:o|a|the) )?$")
_SINCE_LEADING = re.compile(rf"(?:{_SINCE_WORDS})\b")


def fold(text: str) -> str:
    """Lowercase, accents stripped, whitespace collapsed.

    Accent-insensitive because people type "mes passado" and "terca" as
    often as "mês passado" and "terça", on phones that do not help.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(bare.split())


# --- resolvers ----------------------------------------------------------
#
# Each takes the match and the local date of "now", and returns local days.
# None means the words matched but name no real day ("31/02").


def _days_ago(days: int, label: str) -> _Resolver:
    def resolve(_: re.Match[str], today: date) -> _Range:
        day = today - timedelta(days=days)
        return _Range(day, day + timedelta(days=1), label)

    return resolve


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _this_week(_: re.Match[str], today: date) -> _Range:
    monday = _monday(today)
    return _Range(monday, monday + timedelta(days=7), "this_week")


def _last_week(_: re.Match[str], today: date) -> _Range:
    monday = _monday(today)
    return _Range(monday - timedelta(days=7), monday, "last_week")


def _next_month(first: date) -> date:
    return date(first.year + first.month // 12, first.month % 12 + 1, 1)


def _previous_month(first: date) -> date:
    return date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)


def _this_month(_: re.Match[str], today: date) -> _Range:
    first = today.replace(day=1)
    return _Range(first, _next_month(first), "this_month")


def _last_month(_: re.Match[str], today: date) -> _Range:
    this_first = today.replace(day=1)
    return _Range(_previous_month(this_first), this_first, "last_month")


def _last_days(days: int | None = None) -> _Resolver:
    """The last N days, today counted as one of them, up to now."""

    def resolve(match: re.Match[str], today: date) -> _Range | None:
        n = days if days is not None else _number(match["n"])
        if not 1 <= n <= MAX_DAYS:
            return None
        return _Range(today - timedelta(days=n - 1), None, f"last_{n}_days")

    return resolve


def _number(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def _since_weekday(match: re.Match[str], today: date) -> _Range:
    """The most recent such day strictly before today, up to now.

    Strictly before: "desde segunda" said on a Monday means last Monday. It
    would be an odd way to say "today".
    """
    weekday = _WEEKDAYS[match["weekday"]]
    back = (today.weekday() - weekday) % 7 or 7
    label = f"since:{_WEEKDAY_LABELS[weekday]}"
    return _Range(today - timedelta(days=back), None, label)


def _month(match: re.Match[str], today: date) -> _Range:
    """A named month: this year's, or last year's if it has not come yet."""
    month = _MONTHS[match["month"]]
    if match["year"]:
        year = int(match["year"])
    else:
        year = today.year if month <= today.month else today.year - 1
    first = date(year, month, 1)
    return _Range(first, _next_month(first), f"{year:04d}-{month:02d}")


def _one_day(day: date) -> _Range:
    return _Range(day, day + timedelta(days=1), day.isoformat())


#: What may come right before a year-less dd/mm for it to be read as a date:
#: "no dia 21/09", "em 21/09", "since 21/09", "até 23/09", or nothing at all.
_DATE_CUE_BEFORE = re.compile(
    rf"(?:^\W*|\b(?:dia|em|no|on|ate|until|till|from|entre|between|{_SINCE_WORDS})"
    r" (?:(?:o|a|the) )?)$"
)


def _day_month(match: re.Match[str], today: date) -> _Range | None:
    """dd/mm or dd/mm/yyyy. Day first: the deployment reads dates the Brazilian
    way, and 09/10 is the ninth of October. Without a year, a date that has
    not come yet this year is last year's.

    A year-less n/m in the middle of a sentence is as often a fraction or a
    score ("1/2 ETH", "3/4 of the quorum", "score 10/10") as a day, so it is
    only a date after a date cue or as the question's first words.
    """
    cued = match["dia"] or _DATE_CUE_BEFORE.search(match.string[: match.start()])
    if not (match["year"] or cued):
        return None
    day, month = int(match["day"]), int(match["month"])
    year = _full_year(match["year"]) if match["year"] else today.year
    try:
        found = date(year, month, day)
        if not match["year"] and found > today:
            found = date(year - 1, month, day)
    except ValueError:
        return None
    return _one_day(found)


def _full_year(token: str) -> int:
    return 2000 + int(token) if len(token) == 2 else int(token)


def _iso_date(match: re.Match[str], _: date) -> _Range | None:
    try:
        return _one_day(date(int(match["year"]), int(match["month"]), int(match["day"])))
    except ValueError:
        return None


def _day_of_month(match: re.Match[str], today: date) -> _Range | None:
    """"dia 15": this month's, or last month's if it has not come yet."""
    day = int(match["day"])
    first = today.replace(day=1)
    if day > today.day:
        first = _previous_month(first)
    try:
        return _one_day(first.replace(day=day))
    except ValueError:
        return None


# --- rules ----------------------------------------------------------------


def _rule(pattern: str, resolve: _Resolver) -> _Rule:
    return _Rule(re.compile(pattern), resolve)


_WEEKDAY = rf"(?P<weekday>{_alternation(_WEEKDAYS)})(?:(?:-| )feira)?"
_MONTH = rf"(?P<month>{_alternation(_MONTHS)})"
#: Years a chat message plausibly means; also keeps date arithmetic in range.
_YEAR = r"(?:19|20)\d{2}"
#: A quantity after n/m makes it a fraction, never a day: "em 1/2 ETH".
_UNITS = r"eth|weth|btc|sol|usdc|usdt|dai|tokens?"
_N = rf"(?P<n>\d{{1,4}}|{_alternation(_NUMBER_WORDS)})"

# Matched against folded text. Order does not matter: the longest match wins
# (see `_best`), so "day before yesterday" is never read as "yesterday".
_RULES: tuple[_Rule, ...] = (
    _rule(r"\b(?:hoje|today)\b", _days_ago(0, "today")),
    _rule(r"\b(?:ontem|yesterday)\b", _days_ago(1, "yesterday")),
    _rule(
        r"\b(?:anteontem|antes de ontem|(?:the )?day before yesterday)\b",
        _days_ago(2, "day_before_yesterday"),
    ),
    _rule(r"\b(?:(?:n?esta|n?essa) semana|semana atual|this week)\b", _this_week),
    _rule(
        r"\b(?:semana (?:passada|anterior)|(?:last|previous) week)\b", _last_week
    ),
    _rule(r"\b(?:(?:n?este|n?esse) mes|mes atual|this month)\b", _this_month),
    _rule(r"\b(?:mes (?:passado|anterior)|(?:last|previous) month)\b", _last_month),
    _rule(rf"\b(?:ultimos|last|past) {_N} (?:dias|days)\b", _last_days()),
    _rule(r"\bpast week\b", _last_days(7)),
    _rule(r"\bpast month\b", _last_days(30)),
    _rule(rf"\b(?:{_SINCE_WORDS}) (?:(?:a|o|the|last) )?{_WEEKDAY}\b", _since_weekday),
    # A month needs a preposition: bare "may" is a verb, and "o evento de
    # setembro" is a topic, not a filter on when something was said.
    _rule(
        rf"\b(?:em|in|during|no mes de|{_SINCE_WORDS}) {_MONTH}"
        rf"(?: (?:de |of )?(?P<year>{_YEAR}))?\b",
        _month,
    ),
    _rule(
        r"(?<![\d/])(?P<dia>\bdia )?\b(?P<day>\d{1,2})/(?P<month>\d{1,2})"
        rf"(?:/(?P<year>{_YEAR}|\d{{2}}))?\b(?!/)(?! ?(?:%|{_UNITS}\b))",
        _day_month,
    ),
    _rule(
        rf"(?<![\d-])\b(?P<year>{_YEAR})-(?P<month>\d{{2}})-(?P<day>\d{{2}})\b(?!-)",
        _iso_date,
    ),
    _rule(r"\bdia (?P<day>\d{1,2})\b(?!/)", _day_of_month),
)


class _Found(NamedTuple):
    match: re.Match[str]
    range: _Range


#: A span negated in passing ("semana passada, não esta semana") is not asked.
_NEGATED_BEFORE = re.compile(r"\b(?:nao|not) $")
#: A span that bounds a range ("até ontem", "entre 21/09 e 23/09") is only one
#: end of what was asked.
_RANGE_BEFORE = re.compile(
    r"\b(?:ate|until|till|through|entre|between) (?:(?:o|a|the) )?$"
)
_RANGE_AFTER = re.compile(
    r"^ (?:ate|until|till|through|thru)\b"
    rf"|^ (?:to|a) (?:(?:o|a|the) )?(?:dia )?(?:\d|{_alternation(_WEEKDAYS)}\b)"
)


def _best(folded: str, today: date) -> _Found | None:
    """The longest phrase that names a real span; the earliest on a tie.

    None when the question names more than that one span: two different
    spans ("hoje e ontem") or a range ("desde segunda até quarta"). Keeping
    one of them would search a narrower span than was asked, and miss
    evidence quietly; None leaves the question to the ordinary path.
    """
    found = [
        _Found(match, span)
        for rule in _RULES
        for match in rule.pattern.finditer(folded)
        if (span := rule.resolve(match, today)) is not None
        and not _NEGATED_BEFORE.search(folded[: match.start()])
    ]
    if not found:
        return None
    best = found[0]
    for candidate in found[1:]:
        if _longer(candidate.match, best.match):
            best = candidate
    if any(_conflicts(other, best) for other in found):
        return None
    if _bounds_a_range(folded, best.match):
        return None
    return best


def _conflicts(other: _Found, best: _Found) -> bool:
    """Whether `other` is a second span, not a piece of `best` or a repeat."""
    start, end = best.match.span()
    inside = start <= other.match.start() and other.match.end() <= end
    return not inside and other.range != best.range


def _bounds_a_range(folded: str, match: re.Match[str]) -> bool:
    return bool(
        _RANGE_BEFORE.search(folded[: match.start()])
        or _RANGE_AFTER.search(folded[match.end() :])
    )


def _longer(match: re.Match[str], than: re.Match[str]) -> bool:
    length, other = len(match[0]), len(than[0])
    return length > other or (length == other and match.start() < than.start())


def _opened(folded: str, match: re.Match[str]) -> bool:
    """Whether a since-word turns the span into "from then up to now"."""
    return bool(
        _SINCE_BEFORE.search(folded[: match.start()]) or _SINCE_LEADING.match(match[0])
    )


def span_phrases(folded: str, today: date) -> tuple[tuple[int, int], ...]:
    """Where already-folded text names a real day or span, as (start, end).

    Every phrase, not just the one `parse_span` would pick, with a since-word
    right before it included -- for a caller that wants the rest of the
    sentence without the time in it ("o que ela disse sobre o deploy").
    """
    found = []
    for rule in _RULES:
        for match in rule.pattern.finditer(folded):
            if rule.resolve(match, today) is None:
                continue
            opened = _SINCE_BEFORE.search(folded[: match.start()])
            found.append((opened.start() if opened else match.start(), match.end()))
    return tuple(sorted(found))


#: A weekday, or a day of a month ("5 de setembro", "september 5"), that no
#: rule turned into a span: "on monday", "de segunda a quarta", "1 a 5 de
#: setembro". A month alone is left out, because "o evento de setembro" is a
#: topic, not a filter on when something was said.
_UNRESOLVED_TIME = re.compile(
    rf"\b(?:{_alternation(_WEEKDAYS)})\b"
    rf"|\b\d{{1,2}} (?:de |of )?(?:{_alternation(_MONTHS)})\b"
    rf"|\b(?:{_alternation(_MONTHS)}) \d{{1,2}}\b"
)


def names_unresolved_time(folded: str) -> bool:
    """Whether already-folded text still names a day after its spans are cut.

    For a caller that blanked out `span_phrases` and must not quietly drop a
    time it could not read: searching all of history for "o que ele disse de
    segunda a quarta" is a wider search than was asked, stated as if bounded.
    """
    return _UNRESOLVED_TIME.search(folded) is not None


def _midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def parse_span(text: str, now: datetime, tz: tzinfo) -> Span | None:
    """The span `text` names, or None when it names none.

    `now` must be timezone-aware. A span that would end before it starts --
    "desde 25/12" asked in September with the year spelled out -- is None, so
    no caller searches an empty or inverted range.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    folded = fold(text)
    found = _best(folded, now.astimezone(tz).date())
    if found is None:
        return None
    local = found.range
    label = local.label
    if local.after is not None and _opened(folded, found.match):
        local = _Range(local.first, None, local.label)
        label = f"since:{label}"
    start = _midnight(local.first, tz)
    end = now.astimezone(UTC) if local.after is None else _midnight(local.after, tz)
    if start >= end:
        return None
    return Span(start, end, label)


# --- the rest of the sentence ---------------------------------------------
#
# A route that reads "about what" out of a question needs the time taken out
# of it first: "o que ela disse sobre o deploy semana passada" is a question
# about the deploy, not about "o deploy semana passada".


@dataclass(frozen=True, slots=True)
class Cut:
    """A question with every time phrase blanked out, and the span it named.

    Blanked with spaces rather than removed, so an offset matched in `folded`
    cuts the same words out of `typed`, which keeps the accents and capitals
    they were written with.
    """

    #: Folded (see `fold`) and blanked: what a route matches its shapes on.
    folded: str
    #: As typed, blanked the same way. The folded text instead when folding
    #: moved offsets, which nothing typed on a keyboard does.
    typed: str
    #: None when the question named no time.
    span: Span | None


def _blank(text: str, phrases: Sequence[tuple[int, int]]) -> str:
    """`text` with each phrase replaced by spaces, so offsets stay put."""
    for start, end in phrases:
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def cut_span(text: str, now: datetime, tz: tzinfo) -> Cut | None:
    """`text` with its time taken out, or None when that time is unreadable.

    None for a question that names a range or two spans, which `parse_span`
    refuses, and for one that names a day no rule can read ("de segunda a
    quarta", "on monday"): a route that dropped it would answer about all of
    history under words that read as if bounded.
    """
    typed = " ".join(text.split())
    folded = fold(typed)
    source = typed if len(folded) == len(typed) else folded
    phrases = span_phrases(folded, now.astimezone(tz).date())
    span = parse_span(typed, now, tz)
    if phrases and span is None:
        return None
    blanked = _blank(folded, phrases)
    if names_unresolved_time(blanked):
        return None
    return Cut(blanked, _blank(source, phrases), span)


#: A span cut out of "sobre o deploy da semana passada" leaves "o deploy da",
#: and one cut out of "about X in the last 3 days" leaves "X in the". An
#: article goes only with the link before it, so "o plano a" keeps its "a".
_TRAILING_LINK = re.compile(
    r"\s+(?:de|da|do|na|no|em|in|on|at|from|during|durante|this|last|of)"
    r"(?:\s+(?:the|o|a|os|as))?$",
    re.IGNORECASE,
)


def cut_topic(typed: str) -> str:
    """A topic cut from a `Cut`, without the words a removed span left behind."""
    topic = " ".join(typed.split()).strip(" ?.!,")
    while (trimmed := _TRAILING_LINK.sub("", topic).strip()) != topic:
        topic = trimmed
    return topic
