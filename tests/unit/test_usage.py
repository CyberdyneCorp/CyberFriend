"""The usage view's rules: exclusion every request, a short cache, text after the notice."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from chatmemory.app.usage import (
    MAX_WINDOW_DAYS,
    BadWindow,
    Grouping,
    UsageService,
    page_of,
    window_of,
)
from chatmemory.ports.usage import (
    ModelCall,
    ToolCall,
    TraceCount,
    TracedQuestion,
    UsageExclusions,
    UsageRows,
    UsageUnavailable,
    ViewedPerson,
    VoiceUsage,
)
from tests.unit.usage_fakes import FakeUsageDirectory, FakeUsageSource

TODAY = date(2026, 9, 26)
WINDOW = window_of("2026-09-01", "2026-09-26", TODAY)
ANA, BEA, CAL = "101", "202", "303"
D1, D2 = date(2026, 9, 10), date(2026, 9, 20)


def _rows() -> UsageRows:
    return UsageRows(
        traces=(
            TraceCount(ANA, D1, "corpus.fixed", 3),
            TraceCount(ANA, D2, "market.price", 1),
            TraceCount(BEA, D1, "corpus.fixed", 2),
            TraceCount(BEA, D2, "corpus.fixed", 4),
        ),
        models=(
            ModelCall(ANA, D1, "corpus.fixed", "gpt-5.4-mini", 300, 30, 0.03),
            ModelCall(ANA, D2, "market.price", "gpt-5.4-mini", 100, 10, 0.01),
            ModelCall(BEA, D1, "corpus.fixed", "gpt-5.4-mini", 200, 20, 0.02),
            ModelCall(BEA, D2, "corpus.fixed", "chat-v1", 400, 40, 0.0),
        ),
        tools=(
            ToolCall(ANA, D2, "market.price", "market:price", 1),
            ToolCall(BEA, D2, "corpus.fixed", "issues:search", 2),
        ),
    )


class Ticks:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _service(
    source: FakeUsageSource, directory: FakeUsageDirectory, ticks: Ticks | None = None
) -> UsageService:
    return UsageService(source, directory, monotonic=ticks or Ticks())


# --- the window -------------------------------------------------------------


def test_the_default_window_is_the_last_thirty_days_in_whole_days() -> None:
    window = window_of(None, None, TODAY)
    assert window.start == datetime(2026, 8, 28, tzinfo=UTC)
    assert window.end == datetime(2026, 9, 27, tzinfo=UTC)
    assert window.days == 30


def test_ninety_days_is_allowed_and_ninety_one_is_refused() -> None:
    assert window_of("2026-06-29", "2026-09-26", TODAY).days == MAX_WINDOW_DAYS
    with pytest.raises(BadWindow, match="at most 90 days"):
        window_of("2026-06-28", "2026-09-26", TODAY)


@pytest.mark.parametrize(("start", "end"), [("2026-09-10", "2026-09-01"), ("yesterday", None)])
def test_a_reversed_or_malformed_window_is_refused(start: str, end: str | None) -> None:
    with pytest.raises(BadWindow):
        window_of(start, end, TODAY)


@pytest.mark.parametrize(("raw", "page"), [(None, 1), ("0", 1), ("-2", 1), ("x", 1), ("3", 3)])
def test_a_page_is_one_based(raw: str | None, page: int) -> None:
    assert page_of(raw) == page


# --- the summary --------------------------------------------------------------


async def test_per_person_counts_tokens_cost_tools_and_names() -> None:
    directory = FakeUsageDirectory(
        display={ANA: "Ana"}, voice_usage=VoiceUsage(by_user={BEA: 90}, anonymous=30)
    )
    summary = await _service(FakeUsageSource(rows=_rows()), directory).summary(
        WINDOW, Grouping.PERSON
    )

    by_key = {line.key: line for line in summary.lines}
    assert (by_key[ANA].name, by_key[ANA].questions, by_key[ANA].input_tokens) == ("Ana", 4, 400)
    assert by_key[ANA].cost == pytest.approx(0.04)
    assert (by_key[ANA].tool_calls, by_key[ANA].tools) == (1, {"market:price"})
    # No name on record: shown by platform id.
    assert (by_key[BEA].name, by_key[BEA].questions, by_key[BEA].voice_seconds) == (None, 6, 90)
    assert summary.totals.questions == 10
    assert summary.totals.voice_seconds == 120  # the anonymous erased total counts here only
    assert summary.label == "traced question runs"


async def test_per_feature_carries_voice_as_its_own_feature() -> None:
    directory = FakeUsageDirectory(voice_usage=VoiceUsage(by_user={BEA: 90}, anonymous=30))
    summary = await _service(FakeUsageSource(rows=_rows()), directory).summary(
        WINDOW, Grouping.FEATURE
    )

    by_key = {line.key: line for line in summary.lines}
    assert by_key["corpus.fixed"].questions == 9
    assert by_key["corpus.fixed"].tools == {"issues:search"}
    assert by_key["market.price"].questions == 1
    assert by_key["voice"].voice_seconds == 120
    assert by_key["voice"].questions is None


async def test_per_model_and_per_tool_carry_only_what_applies() -> None:
    service = _service(FakeUsageSource(rows=_rows()), FakeUsageDirectory())

    models = {line.key: line for line in (await service.summary(WINDOW, Grouping.MODEL)).lines}
    tools = {line.key: line for line in (await service.summary(WINDOW, Grouping.TOOL)).lines}

    assert models["chat-v1"].input_tokens == 400
    assert models["chat-v1"].questions is None
    assert tools["issues:search"].tool_calls == 2
    assert tools["issues:search"].input_tokens is None


async def test_opted_out_and_erasing_people_are_never_counted() -> None:
    directory = FakeUsageDirectory(
        exclusion=UsageExclusions(people=frozenset({ANA})),
        voice_usage=VoiceUsage(by_user={ANA: 60, BEA: 90}),
    )
    summary = await _service(FakeUsageSource(rows=_rows()), directory).summary(
        WINDOW, Grouping.PERSON
    )

    assert [line.key for line in summary.lines] == [BEA]
    assert summary.totals.questions == 6
    assert summary.totals.voice_seconds == 90


async def test_an_erased_persons_runs_up_to_and_including_that_day_are_dropped() -> None:
    erased = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)
    directory = FakeUsageDirectory(exclusion=UsageExclusions(erased_before={BEA: erased}))
    summary = await _service(FakeUsageSource(rows=_rows()), directory).summary(
        WINDOW, Grouping.PERSON
    )

    bea = next(line for line in summary.lines if line.key == BEA)
    assert bea.questions == 4  # the D1 runs (the day of the erasure) are gone
    assert bea.input_tokens == 400


async def test_pending_deletions_are_left_out_by_the_stores_query_and_key_the_cache() -> None:
    source = FakeUsageSource(rows=_rows())
    directory = FakeUsageDirectory()
    service = _service(source, directory)

    await service.summary(WINDOW, Grouping.PERSON)
    directory.exclusion = UsageExclusions(trace_ids=frozenset({"t-1"}))
    await service.summary(WINDOW, Grouping.PERSON)

    assert [ids for _, ids in source.aggregate_calls] == [frozenset(), frozenset({"t-1"})]


async def test_rows_are_cached_for_five_minutes_and_exclusion_is_not() -> None:
    source = FakeUsageSource(rows=_rows())
    directory = FakeUsageDirectory()
    ticks = Ticks()
    service = _service(source, directory, ticks)

    await service.summary(WINDOW, Grouping.PERSON)
    await service.summary(WINDOW, Grouping.FEATURE)  # same window: cached
    # Ana opts out while the rows are cached: the next request drops her.
    directory.exclusion = UsageExclusions(people=frozenset({ANA}))
    cached = await service.summary(WINDOW, Grouping.PERSON)
    ticks.now += 299
    await service.summary(WINDOW, Grouping.PERSON)
    ticks.now += 1
    await service.summary(WINDOW, Grouping.PERSON)

    assert ANA not in {line.key for line in cached.lines}
    assert len(source.aggregate_calls) == 2


async def test_a_store_that_is_down_is_unavailable_not_zero() -> None:
    service = _service(FakeUsageSource(down=True), FakeUsageDirectory())
    with pytest.raises(UsageUnavailable):
        await service.summary(WINDOW, Grouping.PERSON)


# --- questions ------------------------------------------------------------------


def _question(trace_id: str, at: datetime, text: str) -> TracedQuestion:
    return TracedQuestion(trace_id, at, "corpus.fixed", (), text, 10, 5, 0.001)


NOTICE = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _held() -> dict[str, list[TracedQuestion]]:
    return {
        ANA: [
            _question("before-1", NOTICE - timedelta(days=3), "asked before the notice"),
            _question("before-2", NOTICE - timedelta(seconds=1), "the one that earned it"),
            _question("after-1", NOTICE + timedelta(hours=1), "after the notice"),
            _question("after-2", NOTICE + timedelta(days=2), "withdrawn later"),
        ]
    }


def _ana(notice: datetime | None = NOTICE) -> ViewedPerson:
    return ViewedPerson(person_id=7, display_name="Ana", notice_at=notice)


async def test_only_questions_after_the_notice_are_shown_and_earlier_ones_counted() -> None:
    service = _service(FakeUsageSource(questions_held=_held()), FakeUsageDirectory())

    view = await service.questions(ANA, _ana(), WINDOW, 1)

    assert [q.question for q in view.questions] == ["withdrawn later", "after the notice"]
    assert view.hidden_before_notice == 2


async def test_a_person_never_told_has_no_readable_text() -> None:
    source = FakeUsageSource(questions_held=_held())
    view = await _service(source, FakeUsageDirectory()).questions(ANA, _ana(None), WINDOW, 1)

    assert view.questions == ()
    assert view.hidden_before_notice == 4
    assert source.question_calls == []


async def test_an_unknown_person_has_no_readable_text() -> None:
    source = FakeUsageSource(questions_held=_held())
    view = await _service(source, FakeUsageDirectory()).questions(ANA, None, WINDOW, 1)

    assert view.questions == ()
    assert source.question_calls == []


async def test_a_pending_deletion_is_not_shown_or_counted() -> None:
    directory = FakeUsageDirectory(
        exclusion=UsageExclusions(trace_ids=frozenset({"after-2", "before-1"}))
    )
    view = await _service(FakeUsageSource(questions_held=_held()), directory).questions(
        ANA, _ana(), WINDOW, 1
    )

    assert [q.question for q in view.questions] == ["after the notice"]
    assert view.hidden_before_notice == 1


@pytest.mark.parametrize("excluded", ["opted out", "erasing"])
async def test_an_excluded_person_gets_an_empty_page(excluded: str) -> None:
    source = FakeUsageSource(questions_held=_held())
    directory = FakeUsageDirectory(exclusion=UsageExclusions(people=frozenset({ANA})))

    view = await _service(source, directory).questions(ANA, _ana(), WINDOW, 1)

    assert (view.questions, view.hidden_before_notice) == ((), 0)
    assert source.question_calls == []


async def test_an_erased_persons_questions_up_to_the_erasure_are_gone() -> None:
    # Erased after the first shown question; told again afterwards.
    erased = NOTICE + timedelta(hours=1)
    held = _held()
    held[ANA].append(_question("new", erased + timedelta(days=3), "asked after erasing"))
    directory = FakeUsageDirectory(exclusion=UsageExclusions(erased_before={ANA: erased}))
    told_again = _ana(erased + timedelta(days=2, hours=23))

    view = await _service(FakeUsageSource(questions_held=held), directory).questions(
        ANA, told_again, WINDOW, 1
    )

    assert [q.question for q in view.questions] == ["asked after erasing"]
    # Only what was asked between the erasure and the new notice is counted.
    assert view.hidden_before_notice == 1


async def test_questions_are_never_cached() -> None:
    source = FakeUsageSource(questions_held=_held())
    service = _service(source, FakeUsageDirectory())

    await service.questions(ANA, _ana(), WINDOW, 1)
    await service.questions(ANA, _ana(), WINDOW, 1)

    assert len(source.question_calls) == 2
