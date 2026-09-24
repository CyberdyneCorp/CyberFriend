"""Time spans named in a question, read in the deployment's calendar.

Every case pins the clock and the zone: the module reads neither itself, and
the boundaries worth testing are exactly where UTC and Sao Paulo disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from chatmemory.app.timespan import MAX_DAYS, fold, parse_span

SAO_PAULO = ZoneInfo("America/Sao_Paulo")

#: Thursday 24 September 2026, 15:00 in Sao Paulo.
NOW = datetime(2026, 9, 24, 15, 0, tzinfo=SAO_PAULO)


def local(*args: int) -> datetime:
    """A Sao Paulo wall-clock time, as the UTC instant it is."""
    return datetime(*args, tzinfo=SAO_PAULO).astimezone(UTC)  # type: ignore[misc]


def bounds(text: str, now: datetime = NOW) -> tuple[datetime, datetime, str]:
    span = parse_span(text, now, SAO_PAULO)
    assert span is not None, f"{text!r} named no span"
    return span.start, span.end, span.label


NOW_UTC = NOW.astimezone(UTC)


@pytest.mark.parametrize(
    ("text", "start", "end", "label"),
    [
        # Days.
        ("o que ela disse hoje?", local(2026, 9, 24), local(2026, 9, 25), "today"),
        ("what happened today", local(2026, 9, 24), local(2026, 9, 25), "today"),
        ("e ontem?", local(2026, 9, 23), local(2026, 9, 24), "yesterday"),
        ("yesterday", local(2026, 9, 23), local(2026, 9, 24), "yesterday"),
        ("anteontem", local(2026, 9, 22), local(2026, 9, 23), "day_before_yesterday"),
        ("antes de ontem", local(2026, 9, 22), local(2026, 9, 23), "day_before_yesterday"),
        (
            "the day before yesterday",
            local(2026, 9, 22),
            local(2026, 9, 23),
            "day_before_yesterday",
        ),
        # Calendar weeks, Monday first.
        ("esta semana", local(2026, 9, 21), local(2026, 9, 28), "this_week"),
        ("nessa semana", local(2026, 9, 21), local(2026, 9, 28), "this_week"),
        ("this week", local(2026, 9, 21), local(2026, 9, 28), "this_week"),
        ("semana passada", local(2026, 9, 14), local(2026, 9, 21), "last_week"),
        ("na semana anterior", local(2026, 9, 14), local(2026, 9, 21), "last_week"),
        ("last week", local(2026, 9, 14), local(2026, 9, 21), "last_week"),
        # Calendar months.
        ("este mês", local(2026, 9, 1), local(2026, 10, 1), "this_month"),
        ("this month", local(2026, 9, 1), local(2026, 10, 1), "this_month"),
        ("mês passado", local(2026, 8, 1), local(2026, 9, 1), "last_month"),
        ("last month", local(2026, 8, 1), local(2026, 9, 1), "last_month"),
        # Last N days: today is one of them, and the span runs up to now.
        ("nos últimos 7 dias", local(2026, 9, 18), NOW_UTC, "last_7_days"),
        ("in the last 3 days", local(2026, 9, 22), NOW_UTC, "last_3_days"),
        ("últimos dois dias", local(2026, 9, 23), NOW_UTC, "last_2_days"),
        ("the past week", local(2026, 9, 18), NOW_UTC, "last_7_days"),
        # Since a weekday: the most recent one before today, up to now.
        ("desde segunda", local(2026, 9, 21), NOW_UTC, "since:monday"),
        ("desde a terça-feira", local(2026, 9, 22), NOW_UTC, "since:tuesday"),
        ("since Monday", local(2026, 9, 21), NOW_UTC, "since:monday"),
        ("since last friday", local(2026, 9, 18), NOW_UTC, "since:friday"),
        ("desde quinta", local(2026, 9, 17), NOW_UTC, "since:thursday"),
        # Named months: a month not yet reached this year is last year's.
        ("em setembro", local(2026, 9, 1), local(2026, 10, 1), "2026-09"),
        ("in September", local(2026, 9, 1), local(2026, 10, 1), "2026-09"),
        ("em março", local(2026, 3, 1), local(2026, 4, 1), "2026-03"),
        ("em dezembro", local(2025, 12, 1), local(2026, 1, 1), "2025-12"),
        ("no mês de outubro", local(2025, 10, 1), local(2025, 11, 1), "2025-10"),
        ("em dezembro de 2024", local(2024, 12, 1), local(2025, 1, 1), "2024-12"),
        # Dates, day first.
        ("21/09", local(2026, 9, 21), local(2026, 9, 22), "2026-09-21"),
        ("no dia 21/09", local(2026, 9, 21), local(2026, 9, 22), "2026-09-21"),
        ("21/09/2025", local(2025, 9, 21), local(2025, 9, 22), "2025-09-21"),
        ("21/09/25", local(2025, 9, 21), local(2025, 9, 22), "2025-09-21"),
        ("09/10", local(2025, 10, 9), local(2025, 10, 10), "2025-10-09"),
        ("2026-09-21", local(2026, 9, 21), local(2026, 9, 22), "2026-09-21"),
        ("dia 15", local(2026, 9, 15), local(2026, 9, 16), "2026-09-15"),
        ("dia 30", local(2026, 8, 30), local(2026, 8, 31), "2026-08-30"),
    ],
)
def test_named_spans(text: str, start: datetime, end: datetime, label: str) -> None:
    assert bounds(text) == (start, end, label)


# --- since ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "start", "label"),
    [
        ("desde ontem", local(2026, 9, 23), "since:yesterday"),
        ("since yesterday", local(2026, 9, 23), "since:yesterday"),
        ("desde a semana passada", local(2026, 9, 14), "since:last_week"),
        ("a partir do mês passado", local(2026, 8, 1), "since:last_month"),
        ("desde setembro", local(2026, 9, 1), "since:2026-09"),
        ("desde o dia 21/09", local(2026, 9, 21), "since:2026-09-21"),
    ],
)
def test_since_opens_the_span_up_to_now(text: str, start: datetime, label: str) -> None:
    assert bounds(text) == (start, NOW_UTC, label)


def test_since_needs_to_be_next_to_the_span() -> None:
    """"desde" earlier in the sentence is about something else."""
    _, end, label = bounds("desde que ele entrou, o que disse ontem?")
    assert (end, label) == (local(2026, 9, 24), "yesterday")


def test_a_since_span_that_has_not_started_names_nothing() -> None:
    assert parse_span("desde 2026-12-25", NOW, SAO_PAULO) is None


# --- the deployment's calendar, not UTC's ---------------------------------


def test_a_message_at_2330_brt_counts_as_yesterday() -> None:
    """23:30 in Sao Paulo is already the next day in UTC."""
    message = local(2026, 9, 23, 23, 30)
    assert message.date().isoformat() == "2026-09-24"
    start, end, _ = bounds("ontem")
    assert start <= message < end


def test_today_asked_late_in_the_evening_is_the_local_day() -> None:
    evening = datetime(2026, 9, 24, 22, 0, tzinfo=SAO_PAULO)
    assert evening.astimezone(UTC).day == 25
    assert bounds("hoje", evening)[:2] == (local(2026, 9, 24), local(2026, 9, 25))


def test_the_month_is_the_local_month_on_its_last_evening() -> None:
    last_evening = datetime(2026, 9, 30, 22, 0, tzinfo=SAO_PAULO)
    assert last_evening.astimezone(UTC).month == 10
    assert bounds("este mês", last_evening)[:2] == (local(2026, 9, 1), local(2026, 10, 1))


def test_sao_paulo_keeps_minus_three_all_year() -> None:
    """No daylight saving since 2019: January, once summer time, is -03:00."""
    summer = datetime(2027, 1, 20, 12, 0, tzinfo=SAO_PAULO)
    start, end, _ = bounds("ontem", summer)
    assert start == datetime(2027, 1, 19, 3, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=24)


def test_a_zone_with_daylight_saving_gets_the_long_day() -> None:
    """The arithmetic is on calendar days, so a 25-hour day stays one day."""
    new_york = ZoneInfo("America/New_York")
    after_change = datetime(2026, 11, 2, 12, 0, tzinfo=new_york)
    span = parse_span("yesterday", after_change, new_york)
    assert span is not None
    assert span.end - span.start == timedelta(hours=25)


def test_the_zone_changes_the_day_not_just_the_offset() -> None:
    """At 01:30 UTC it is still the previous evening in Sao Paulo."""
    now = datetime(2026, 9, 24, 1, 30, tzinfo=UTC)
    in_utc = parse_span("hoje", now, UTC)
    in_sp = parse_span("hoje", now, SAO_PAULO)
    assert in_utc is not None and in_sp is not None
    assert in_utc.start == datetime(2026, 9, 24, tzinfo=UTC)
    assert in_sp.start == local(2026, 9, 23)


# --- week and month boundaries ----------------------------------------------


def test_this_week_on_a_monday_just_after_midnight() -> None:
    monday = datetime(2026, 9, 21, 0, 10, tzinfo=SAO_PAULO)
    assert bounds("esta semana", monday)[:2] == (local(2026, 9, 21), local(2026, 9, 28))
    assert bounds("semana passada", monday)[:2] == (local(2026, 9, 14), local(2026, 9, 21))


def test_this_week_on_a_sunday_night() -> None:
    sunday = datetime(2026, 9, 27, 23, 59, tzinfo=SAO_PAULO)
    assert bounds("this week", sunday)[:2] == (local(2026, 9, 21), local(2026, 9, 28))


def test_last_month_on_the_first_of_the_month() -> None:
    first = datetime(2026, 3, 1, 0, 30, tzinfo=SAO_PAULO)
    assert bounds("mês passado", first)[:2] == (local(2026, 2, 1), local(2026, 3, 1))


def test_dia_31_in_a_month_after_a_short_one_names_nothing() -> None:
    """1 March: "dia 31" has not come, and February has no 31st."""
    march = datetime(2026, 3, 1, 12, 0, tzinfo=SAO_PAULO)
    assert parse_span("dia 31", march, SAO_PAULO) is None


# --- year rollover ------------------------------------------------------------

#: Saturday 2 January 2027.
NEW_YEAR = datetime(2027, 1, 2, 10, 0, tzinfo=SAO_PAULO)


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("semana passada", local(2026, 12, 21), local(2026, 12, 28)),
        ("esta semana", local(2026, 12, 28), local(2027, 1, 4)),
        ("mês passado", local(2026, 12, 1), local(2027, 1, 1)),
        ("este mês", local(2027, 1, 1), local(2027, 2, 1)),
        ("em dezembro", local(2026, 12, 1), local(2027, 1, 1)),
        ("em janeiro", local(2027, 1, 1), local(2027, 2, 1)),
        ("25/12", local(2026, 12, 25), local(2026, 12, 26)),
        ("dia 31", local(2026, 12, 31), local(2027, 1, 1)),
        ("anteontem", local(2026, 12, 31), local(2027, 1, 1)),
    ],
)
def test_year_rollover(text: str, start: datetime, end: datetime) -> None:
    assert bounds(text, NEW_YEAR)[:2] == (start, end)


def test_last_days_reach_back_into_last_year() -> None:
    start, end, _ = bounds("últimos 5 dias", NEW_YEAR)
    assert (start, end) == (local(2026, 12, 29), NEW_YEAR.astimezone(UTC))


# --- reading -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [
        ("mes passado", "mês passado"),
        ("MÊS PASSADO", "mês passado"),
        ("desde terca", "desde terça"),
        ("desde Terça-Feira", "desde terça"),
        ("em marco", "em março"),
        ("ultimos   7\tdias", "últimos 7 dias"),
    ],
)
def test_accents_case_and_spacing_do_not_matter(typed: str, canonical: str) -> None:
    assert parse_span(typed, NOW, SAO_PAULO) == parse_span(canonical, NOW, SAO_PAULO)


def test_fold_strips_accents_and_collapses_space() -> None:
    assert fold("  Mês   PASSADO, anteontem é terça ") == "mes passado, anteontem e terca"


@pytest.mark.parametrize(
    ("text", "label"),
    [
        # The longer phrase wins over the one inside it.
        ("anteontem", "day_before_yesterday"),
        ("the day before yesterday", "day_before_yesterday"),
        ("no dia 21/09", "2026-09-21"),
    ],
)
def test_longest_phrase_wins(text: str, label: str) -> None:
    assert bounds(text)[2] == label


@pytest.mark.parametrize(
    "text",
    [
        "o que o João disse sobre o deploy?",
        "what did Bea say about the release",
        # A month needs a preposition: a topic or a verb is not a filter.
        "o evento de setembro",
        "may I ask something",
        # Days that do not exist.
        "31/02",
        "2026-13-01",
        "dia 0",
        # Counts outside what anybody means in days.
        "últimos 0 dias",
        f"last {MAX_DAYS + 1} days",
        # Not dates.
        "https://x.io/a/12/10/b",
        "versão 1.2.3",
        # Fractions and scores: a year-less n/m needs a date cue.
        "quanto custa 1/2 ETH",
        "o que o João disse sobre 1/2 ETH",
        "vote 3/4 of the quorum",
        "score 10/10",
        "the 1/1 meeting",
        "em 1/2 ETH",
        # Two spans, or a range: one end of it would search too narrowly.
        "hoje e ontem",
        "desde segunda até quarta",
        "em 21/09 até 23/09",
        "entre 21/09 e 23/09",
        "21/09 to 23/09",
        "o que ele disse até ontem",
    ],
)
def test_no_span(text: str) -> None:
    assert parse_span(text, NOW, SAO_PAULO) is None


@pytest.mark.parametrize(
    ("text", "label"),
    [
        # After a date cue or as the first words, a year-less dd/mm is a day.
        ("o que o João disse em 21/09?", "2026-09-21"),
        ("what did Bea say on 21/09", "2026-09-21"),
        ("since the 21/09", "since:2026-09-21"),
        ("21/09, o que ela disse?", "2026-09-21"),
        # With a year it is a day anywhere.
        ("o que ela disse 21/09/2025", "2025-09-21"),
    ],
)
def test_a_cued_date_still_reads(text: str, label: str) -> None:
    assert bounds(text)[2] == label


@pytest.mark.parametrize(
    ("text", "label"),
    [
        # "a" after a span is often an article, not the start of a range.
        ("o que ela disse ontem à tarde?", "yesterday"),
        # The same span twice is still one span.
        ("ontem, sim, ontem", "yesterday"),
        # A span negated in passing is not a second span.
        ("semana passada, não esta semana", "last_week"),
    ],
)
def test_one_span_named_more_than_once_is_kept(text: str, label: str) -> None:
    assert bounds(text)[2] == label


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        parse_span("hoje", datetime(2026, 9, 24, 12, 0), SAO_PAULO)


def test_it_reads_no_clock_of_its_own() -> None:
    """The same words at a distant "now" resolve against that now."""
    long_ago = datetime(2020, 2, 29, 12, 0, tzinfo=SAO_PAULO)
    assert bounds("ontem", long_ago)[:2] == (local(2020, 2, 28), local(2020, 2, 29))
