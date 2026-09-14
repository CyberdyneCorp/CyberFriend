"""Retention policy and the pass that applies it.

The properties that matter are the ones an operator relies on without being
able to check: that "no window configured" is distinguishable from "nothing to
purge", that the pass covers documents as well as conversation, and that
running it twice is not more destructive than running it once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chatmemory.app.retention import (
    CorpusPurge,
    RetentionPolicy,
    RetentionService,
)

# No module-level asyncio mark: the policy tests are synchronous, and marking
# them anyway makes pytest-asyncio warn on every one of them.
NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


class FakeCorpus:
    """Counts what a pass asked it to remove, and from when."""

    def __init__(self, purge: CorpusPurge | None = None) -> None:
        self.cutoffs: list[datetime] = []
        self._purge = purge or CorpusPurge(windows=2, messages=5, asks=1, fetch_records=3)

    async def purge_corpus_before(self, cutoff: datetime) -> CorpusPurge:
        self.cutoffs.append(cutoff)
        # Re-runnable: a second pass over the same cutoff finds the rows gone.
        if len(self.cutoffs) > 1:
            return CorpusPurge()
        return self._purge


class FakeDocuments:
    def __init__(self, removed: int = 4) -> None:
        self.cutoffs: list[datetime] = []
        self._removed = removed

    async def purge_documents_before(self, cutoff: datetime) -> int:
        self.cutoffs.append(cutoff)
        if len(self.cutoffs) > 1:
            return 0
        return self._removed


def test_no_window_means_retain_indefinitely() -> None:
    assert RetentionPolicy.from_days(None).cutoff(NOW) is None


def test_the_cutoff_is_the_window_before_now() -> None:
    assert RetentionPolicy.from_days(30).cutoff(NOW) == NOW - timedelta(days=30)


@pytest.mark.parametrize("days", [0, -1])
def test_a_non_positive_window_is_rejected_rather_than_clamped(days: int) -> None:
    """`RETENTION_DAYS=0` is an unset variable far more often than a request
    to delete the entire corpus on the next pass."""
    with pytest.raises(ValueError, match="positive"):
        RetentionPolicy.from_days(days)


async def test_a_disabled_policy_purges_nothing_and_says_so() -> None:
    """Reported, not silently skipped: "not configured" and "found nothing"
    are different operational states."""
    corpus = FakeCorpus()
    service = RetentionService(corpus, RetentionPolicy.from_days(None), FakeDocuments())

    report = await service.run_once(NOW)

    assert not report.ran
    assert report.total == 0
    assert corpus.cutoffs == []


async def test_a_pass_purges_both_corpora_at_the_same_cutoff() -> None:
    """An upload outlives the message that carried it; retention that covers
    conversation and not documents leaves the archive it was meant to remove."""
    corpus, documents = FakeCorpus(), FakeDocuments()
    service = RetentionService(corpus, RetentionPolicy.from_days(30), documents)

    report = await service.run_once(NOW)

    expected = NOW - timedelta(days=30)
    assert corpus.cutoffs == [expected]
    assert documents.cutoffs == [expected]
    assert report.cutoff == expected
    assert report.corpus.messages == 5
    assert report.document_entries == 4
    assert report.total == 15


async def test_a_second_pass_is_a_no_op() -> None:
    corpus, documents = FakeCorpus(), FakeDocuments()
    service = RetentionService(corpus, RetentionPolicy.from_days(30), documents)

    await service.run_once(NOW)
    second = await service.run_once(NOW)

    assert second.ran
    assert second.total == 0


async def test_documents_are_not_silently_uncovered() -> None:
    """Without a document store the pass still runs, and the report shows the
    gap rather than reading as a complete purge."""
    corpus = FakeCorpus()
    service = RetentionService(corpus, RetentionPolicy.from_days(7))

    report = await service.run_once(NOW)

    assert report.ran
    assert report.document_entries == 0
    assert report.corpus.messages == 5
