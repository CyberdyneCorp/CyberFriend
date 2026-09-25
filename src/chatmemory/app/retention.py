"""Retention: the corpus stops being a permanent record of everything said.

Without this, running the bot anywhere real means building a searchable,
indefinitely-retained archive of every conversation the team has had. That is a
governance decision, not an implementation detail, so it is expressed as a
policy object with an explicit window rather than as a cron job somebody wrote
once.

Two properties decide whether a purge is worth anything:

*   **Complete.** Nothing may remain retrievable by any path. A message removed
    from `message` but left inside a `conversation_window` is still returned by
    search, because search reads the window's text and never re-derives it from
    its messages. So windows go first and by `starts_at`, which is the earliest
    content they carry -- a window that straddles the cutoff holds pre-cutoff
    text and must go with it. Documents and their entries are purged in the
    same pass, because an upload outlives the message that carried it.

*   **Re-runnable.** Every statement is a delete keyed on a timestamp, so a
    pass that fails halfway leaves a smaller corpus rather than a corrupt one
    and the next pass finishes the job. Nothing here is incremental and nothing
    records progress; there is no state to get out of step.

The window itself is deliberately not read from the environment in this
module. `RetentionPolicy.from_days` takes the number, and the caller that owns
configuration supplies it -- see docs/operations.md for where that is wired and
what is still missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class CorpusPurge:
    """What one pass removed from the conversation corpus."""

    windows: int = 0
    messages: int = 0
    asks: int = 0
    fetch_records: int = 0
    decisions: int = 0

    @property
    def total(self) -> int:
        return (
            self.windows + self.messages + self.asks + self.fetch_records + self.decisions
        )


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """What one pass removed, and the cutoff it ran against.

    `cutoff` is None when the policy retains indefinitely. That is reported
    rather than silently skipped: "retention is not configured" and "retention
    ran and found nothing" are different operational states, and an operator
    reading a dashboard has to be able to tell them apart.
    """

    cutoff: datetime | None
    corpus: CorpusPurge = CorpusPurge()
    document_entries: int = 0

    @property
    def ran(self) -> bool:
        return self.cutoff is not None

    @property
    def total(self) -> int:
        return self.corpus.total + self.document_entries


class CorpusRetention(Protocol):
    """The conversation corpus's half of a retention pass."""

    async def purge_corpus_before(self, cutoff: datetime) -> CorpusPurge:
        """Remove every message, window, ask and fetch record older than `cutoff`.

        Must leave nothing retrievable: a window carrying pre-cutoff text is
        older than the cutoff whatever its `ends_at` says.
        """
        ...


class DocumentRetention(Protocol):
    """The document corpus's half. Satisfied by the document store."""

    async def purge_documents_before(self, cutoff: datetime) -> int: ...


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long the corpus keeps what it captured.

    `None` means "for ever", which is the status quo and therefore the default:
    turning retention on is a decision an operator makes, and a default that
    silently deleted a team's history would be the worse failure of the two.
    """

    window: timedelta | None = None

    @classmethod
    def from_days(cls, days: int | None) -> RetentionPolicy:
        """Build from a configured number of days; None retains indefinitely.

        Zero and negatives are rejected rather than clamped: `RETENTION_DAYS=0`
        is far more likely to be an unset variable that stringified badly than
        a request to purge the entire corpus on the next pass.
        """
        if days is None:
            return cls(None)
        if days <= 0:
            raise ValueError(
                f"retention window must be a positive number of days, got {days}; "
                "omit it entirely to retain indefinitely"
            )
        return cls(timedelta(days=days))

    def cutoff(self, now: datetime | None = None) -> datetime | None:
        """The instant before which content is no longer retained."""
        if self.window is None:
            return None
        return (now or datetime.now(UTC)) - self.window


class RetentionService:
    """Applies a retention policy to both corpora.

    `documents` is optional only because a deployment may not have the document
    tables wired; when it is absent that is logged at every pass, because a
    retention pass that silently covers conversation and not uploads is the
    kind of half-measure that reads as compliance in a report.
    """

    def __init__(
        self,
        corpus: CorpusRetention,
        policy: RetentionPolicy,
        documents: DocumentRetention | None = None,
    ) -> None:
        self._corpus = corpus
        self._policy = policy
        self._documents = documents

    async def run_once(self, now: datetime | None = None) -> RetentionReport:
        cutoff = self._policy.cutoff(now)
        if cutoff is None:
            log.info("retention.disabled")
            return RetentionReport(cutoff=None)

        purged = await self._corpus.purge_corpus_before(cutoff)

        documents = 0
        if self._documents is None:
            log.warning(
                "retention.documents_not_covered",
                hint="no document store wired; uploads are retained indefinitely",
            )
        else:
            documents = await self._documents.purge_documents_before(cutoff)

        report = RetentionReport(cutoff=cutoff, corpus=purged, document_entries=documents)
        log.info(
            "retention.pass",
            cutoff=cutoff.isoformat(),
            windows=purged.windows,
            messages=purged.messages,
            asks=purged.asks,
            fetch_records=purged.fetch_records,
            decisions=purged.decisions,
            document_entries=documents,
        )
        return report
