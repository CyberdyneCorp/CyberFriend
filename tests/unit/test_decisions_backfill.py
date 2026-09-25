"""The decision backfill's matching and paging, over a fake ledger."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest

from chatmemory.app.decisions.backfill import BackfillReport, DecisionBackfill, ScannedMessage
from chatmemory.entrypoints.decisions_backfill import _arguments, _parser, describe, start_of

SINCE = datetime(2026, 6, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 1, tzinfo=UTC)


class FakeLedger:
    def __init__(self, messages: Sequence[ScannedMessage]) -> None:
        self.messages = list(messages)
        self.scans: list[tuple[frozenset[int], int, int]] = []
        self.windows: list[tuple[datetime, datetime | None]] = []
        self.resets: list[list[int]] = []

    async def live_messages_since(
        self,
        since: datetime,
        until: datetime | None,
        channel_ids: frozenset[int],
        after_id: int,
        limit: int,
    ) -> Sequence[ScannedMessage]:
        self.scans.append((channel_ids, after_id, limit))
        self.windows.append((since, until))
        later = [m for m in self.messages if m.platform_message_id > after_id]
        return later[:limit]

    async def reset_extraction(self, message_ids: Sequence[int]) -> int:
        self.resets.append(list(message_ids))
        return len(message_ids)


def scanned(*contents: str) -> list[ScannedMessage]:
    return [ScannedMessage(n, c) for n, c in enumerate(contents, start=1)]


@pytest.mark.asyncio
async def test_only_messages_the_candidate_filter_would_send_as_decisions_are_reset() -> None:
    ledger = FakeLedger(
        scanned(
            "fechou, deploy na sexta então",
            "bora fazer o deploy na sexta?",
            "a máquina de café voltou",
            "We decided to keep Postgres",
            # "going with" was dropped from the markers; it must not match here
            # either, or the backfill pays for messages extraction declines.
            "going with my family this weekend",
        )
    )

    report = await DecisionBackfill(ledger).run(SINCE, frozenset({10}))

    assert ledger.resets == [[1, 2, 4]]
    assert report == BackfillReport(scanned=5, matched=3, reset=3)


@pytest.mark.asyncio
async def test_the_window_is_read_page_by_page_after_the_last_id() -> None:
    ledger = FakeLedger(scanned("combinado", "nada", "fechado", "decidimos", "oi"))

    report = await DecisionBackfill(ledger, page=2).run(SINCE, frozenset({10}))

    assert [after for _, after, _ in ledger.scans] == [0, 2, 4, 5]
    assert ledger.resets == [[1], [3, 4]], "a page with no match costs no statement"
    assert (report.scanned, report.matched, report.reset) == (5, 3, 3)


@pytest.mark.asyncio
async def test_an_empty_scope_reads_nothing() -> None:
    ledger = FakeLedger(scanned("fechado"))

    report = await DecisionBackfill(ledger).run(SINCE, frozenset())

    assert ledger.scans == []
    assert report == BackfillReport()


@pytest.mark.asyncio
async def test_every_page_is_read_over_the_same_window() -> None:
    ledger = FakeLedger(scanned("combinado", "fechado", "decidimos"))

    await DecisionBackfill(ledger, page=2).run(SINCE, frozenset({10}), UNTIL)
    await DecisionBackfill(ledger).run(SINCE, frozenset({10}))

    assert ledger.windows == [(SINCE, UNTIL)] * 3 + [(SINCE, None)] * 2


def test_until_is_optional_and_starts_at_midnight_utc() -> None:
    assert _arguments(["--since", "2026-06-01"]).until is None
    args = _arguments(["--since", "2026-06-01", "--until", "2026-09-01"])

    assert start_of(args.until) == UNTIL


def test_until_must_come_after_since() -> None:
    with pytest.raises(SystemExit):
        _arguments(["--since", "2026-06-01", "--until", "2026-06-01"])
    with pytest.raises(SystemExit):
        _arguments(["--since", "2026-06-01", "--until", "2026-05-01"])


def test_since_is_a_date_and_starts_at_midnight_utc() -> None:
    args = _parser().parse_args(["--since", "2026-06-01"])

    assert start_of(args.since) == SINCE


def test_since_is_required_and_must_be_a_date() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args([])
    with pytest.raises(SystemExit):
        _parser().parse_args(["--since", "2026-06-01'; DROP TABLE message; --"])


def test_the_summary_says_how_many_were_reset() -> None:
    line = describe(BackfillReport(scanned=40, matched=5, reset=3), date(2026, 6, 1))

    assert line.startswith("reset 3 message(s) since 2026-06-01")
    assert "5 carry a decision marker, 2 were already pending; 40 scanned" in line


def test_the_summary_names_the_end_of_the_window_when_there_is_one() -> None:
    line = describe(BackfillReport(reset=1), date(2026, 6, 1), date(2026, 9, 1))

    assert line.startswith("reset 1 message(s) since 2026-06-01, before 2026-09-01, for")
