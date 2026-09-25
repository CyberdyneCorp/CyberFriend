"""The standing extraction pass, and what it reports about itself.

Extraction runs on ingest rather than on query, so something has to feed it
continuously. This is that something: captured messages are submitted as they
are persisted, buffered per channel into a window-sized batch, and handed to
`ExtractionService`, which pays for a model call only on the messages carrying
a plausible addressee.

Three properties matter more here than the mechanism.

*It cannot block capture.* `submit` never awaits and never raises. A queue
that filled up must cost extractions, never ingestion -- the corpus is the
thing that cannot be rebuilt.

*It cannot die.* A worker that raises on one bad batch stops extracting
entirely, and the only symptom is that obligations quietly stop appearing;
nobody notices, because the answer to "what did people ask me" is a plausible
"nothing outstanding". So every batch is wrapped, exactly as the embedding
worker's is.

*It has to say how it is doing.* A stalled extractor looks identical to a
quiet server from the outside, so the counters below are published on the
health endpoint rather than kept for logs.

`BacklogExtractionWorker` is the other half, and the reason is that the queue
above can only ever hold what arrived live. Everything backfill imports --
most of any channel's history -- was submitted to nothing, so obligations
began at process start. It reads the same messages back out of the corpus,
extracts them through the same worker, and records what it has read so that
neither pass pays for the other's work.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

import structlog

from chatmemory.app.asks.extraction import ExtractionReport, ExtractionService
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.messages import Message
from chatmemory.ports.store import PendingExtraction

log = structlog.get_logger()


class NameLearner(Protocol):
    """Something that learns who people are from what it sees.

    Here so the worker can feed the addressee directory without owning one:
    the names an ask can refer to are the names of the people talking, and the
    only place both the messages and the directory are in scope is here.
    """

    def observe(self, message: Message) -> None: ...


class ExtractionLedger(Protocol):
    """The corpus's record of what extraction has already read.

    Narrower than `Store` on purpose: this pass may read messages back and
    say it has read them, and nothing else. It is the same record from both
    sides -- the live pass marks what it extracts so the backlog pass does
    not pay for it a second time, and the backlog pass finds everything the
    live pass never saw, which is most of a channel's history.
    """

    async def messages_pending_extraction(
        self, limit: int, channels: Sequence[ChannelRef] = ()
    ) -> Sequence[PendingExtraction]:
        ...

    async def record_extraction(self, entries: Sequence[PendingExtraction]) -> int: ...

    async def pending_extraction_count(
        self, cap: int = 1000, channels: Sequence[ChannelRef] = ()
    ) -> int: ...


#: Messages buffered per channel before the batch is handed to extraction.
#: Roughly a window: enough preceding conversation for the model to tell a
#: follow-up from an opener, small enough that a quiet channel is not held
#: hostage by a busy one.
WINDOW_MESSAGES = 20

#: How long a partial batch waits for the messages that would fill it. A
#: channel that goes quiet mid-conversation must still have its asks
#: extracted; without this, the last few messages of every conversation would
#: sit in memory until the next one started.
FLUSH_AFTER_SECONDS = 30.0

#: Backlog bound. Reached only if extraction is slower than traffic for a
#: sustained period, in which case dropping the newest submissions is the
#: honest failure: it is visible in `dropped`, and the alternative is an
#: unbounded buffer that ends the process.
MAX_QUEUED = 5_000

#: Messages kept per channel after a flush, as reply parents and context for
#: the next batch. Small: this is memory held for every active channel.
CARRIED_MESSAGES = 10


@dataclass(frozen=True, slots=True)
class ExtractionProgress:
    """What the pass has done, for the health endpoint.

    `last_flush_at` is the field that makes a stall visible: a queue that is
    not empty while this stops advancing is a worker that has stopped, which
    no count of extracted asks would show.
    """

    queued: int = 0
    buffered: int = 0
    dropped: int = 0
    windows: int = 0
    candidates: int = 0
    extracted: int = 0
    recorded: int = 0
    failed: int = 0
    decisions: int = 0
    decisions_failed: int = 0
    batches_failed: int = 0
    last_flush_at: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "queued": self.queued,
            "buffered": self.buffered,
            "dropped": self.dropped,
            "windows": self.windows,
            "candidates": self.candidates,
            "extracted": self.extracted,
            "recorded": self.recorded,
            "failed": self.failed,
            "decisions": self.decisions,
            "decisions_failed": self.decisions_failed,
            "batches_failed": self.batches_failed,
            "last_flush_at": self.last_flush_at,
        }


@dataclass
class _Buffer:
    """One channel's pending messages, and the tail of the last batch.

    The carried tail is passed as reply *parents* rather than prepended to the
    batch: a message that has already been through extraction must not be
    charged for a second time, but an ask replying to it still has to be able
    to resolve who it was addressed to.
    """

    pending: list[Message] = field(default_factory=list)
    carried: list[Message] = field(default_factory=list)
    opened_at: float = 0.0

    def parents(self) -> Mapping[int, Message]:
        return {m.platform_message_id: m for m in self.carried}


class ExtractionWorker:
    """Turns captured messages into extracted asks, continuously."""

    def __init__(
        self,
        extraction: ExtractionService,
        window_messages: int = WINDOW_MESSAGES,
        flush_after_seconds: float = FLUSH_AFTER_SECONDS,
        max_queued: int = MAX_QUEUED,
        directory: NameLearner | None = None,
    ) -> None:
        self._extraction = extraction
        self._directory = directory
        self._window = window_messages
        self._flush_after = flush_after_seconds
        self._queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=max_queued)
        self._buffers: dict[ChannelRef, _Buffer] = {}
        self._progress = ExtractionProgress()
        self._ledger: ExtractionLedger | None = None

    def records_through(self, ledger: ExtractionLedger) -> None:
        """Mark what this pass extracts, so the backlog pass does not repeat it.

        Set after construction because the composition root builds this worker
        out of the ask pipeline alone, while the ledger is the corpus store the
        ingest entrypoint owns. Left unset -- in tests, or in a process with no
        corpus store -- the worker behaves exactly as it did before: it records
        nothing, and the backlog pass eventually reads those messages again.
        Forgetting this costs model calls, never asks.
        """
        self._ledger = ledger

    # --- the ingest side -------------------------------------------------

    def submit(self, message: Message) -> bool:
        """Offer a freshly captured message. Never blocks, never raises.

        Called from the live loop immediately after the message is persisted,
        because an ask row references the message row: extracting from a
        message the store has not written yet has nowhere to point.
        """
        if self._directory is not None:
            # Learned from every captured message, not only from the ones
            # extraction pays for: somebody who never triggers a candidate can
            # still be the person a later ask names.
            self._directory.observe(message)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._progress = replace(self._progress, dropped=self._progress.dropped + 1)
            # Warned every time rather than once: a full queue means asks are
            # being lost right now, and a single startup-time line would be
            # scrolled past long before anybody looked.
            log.warning(
                "asks.extraction_queue_full",
                message_id=message.platform_message_id,
                dropped=self._progress.dropped,
            )
            return False
        return True

    @property
    def progress(self) -> ExtractionProgress:
        """A snapshot, with the live queue depth folded in."""
        return replace(
            self._progress,
            queued=self._queue.qsize(),
            buffered=sum(len(b.pending) for b in self._buffers.values()),
        )

    # --- the worker side -------------------------------------------------

    async def run_once(self, now: float | None = None) -> int:
        """Drain what has arrived and extract from every batch that is ready.

        Returns the number of messages extracted from, so a caller can tell a
        busy pass from an idle one and sleep accordingly.
        """
        at = now if now is not None else time.monotonic()
        self._drain(at)
        done = 0
        for channel in list(self._buffers):
            if self._ready(channel, at):
                done += await self._flush(channel, at)
        return done

    async def flush_all(self, now: float | None = None) -> int:
        """Extract from every buffered batch, ready or not."""
        at = now if now is not None else time.monotonic()
        self._drain(at)
        done = 0
        for channel in list(self._buffers):
            done += await self._flush(channel, at)
        return done

    def _drain(self, at: float) -> None:
        while True:
            try:
                message = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            buffer = self._buffers.setdefault(message.channel, _Buffer(opened_at=at))
            if not buffer.pending:
                buffer.opened_at = at
            buffer.pending.append(message)

    def _ready(self, channel: ChannelRef, at: float) -> bool:
        buffer = self._buffers[channel]
        if not buffer.pending:
            return False
        return (
            len(buffer.pending) >= self._window
            or at - buffer.opened_at >= self._flush_after
        )

    async def _flush(self, channel: ChannelRef, at: float) -> int:
        buffer = self._buffers[channel]
        batch = buffer.pending
        if not batch:
            return 0
        buffer.pending = []
        buffer.opened_at = at

        if not await self.extract_batch(batch, buffer.parents()):
            return 0

        buffer.carried = (buffer.carried + batch)[-CARRIED_MESSAGES:]
        await self._mark_extracted(batch)
        return len(batch)

    async def extract_batch(
        self, messages: Sequence[Message], parents: Mapping[int, Message] | None = None
    ) -> bool:
        """Extract from one batch, absorbing its failure. True if it ran.

        Public because history does not arrive through the queue. The backlog
        pass reads messages the gateway never delivered, and it has to extract
        them the same way: same service, same counters, same refusal to let one
        bad batch end the pass.
        """
        if not messages:
            return False
        if self._directory is not None:
            # Backfilled authors are names an ask can refer to exactly as much
            # as live ones are, and history is where most of the names come
            # from. Idempotent, so a live batch observed at submit and again
            # here costs nothing.
            for message in messages:
                self._directory.observe(message)

        try:
            report = await self._extraction.extract_window(messages, parents)
        except Exception:
            # One channel's batch must not end the pass. The messages are
            # already persisted, so nothing is lost that a later re-extraction
            # could not recover; what would be lost is every *other* channel's
            # extraction, for good, if this propagated.
            log.exception(
                "asks.extraction_batch_failed", channel=str(messages[0].channel)
            )
            self._progress = replace(
                self._progress, batches_failed=self._progress.batches_failed + 1
            )
            return False

        self._record(report)
        return True

    async def _mark_extracted(self, batch: Sequence[Message]) -> None:
        """Record a live batch as read, with no generation to record it at.

        The live pass is handed messages rather than reading rows, so it has
        no revision to name and the store stamps whatever is current. A
        failure here is absorbed: the cost is that the backlog pass extracts
        these messages again, which is money, while raising would cost the
        extraction of every other channel in this pass.
        """
        if self._ledger is None:
            return
        try:
            await self._ledger.record_extraction(
                [PendingExtraction(message=m) for m in batch]
            )
        except Exception:
            log.exception("asks.extraction_mark_failed", count=len(batch))

    def _record(self, report: ExtractionReport) -> None:
        current = self._progress
        self._progress = replace(
            current,
            windows=current.windows + 1,
            candidates=current.candidates + report.candidates,
            extracted=current.extracted + report.extracted,
            recorded=current.recorded + report.recorded,
            failed=current.failed + report.failed,
            decisions=current.decisions + report.decisions,
            decisions_failed=current.decisions_failed + report.decisions_failed,
            last_flush_at=time.time(),
        )


#: Messages one backlog pass may read. Together with the interval its loop
#: sleeps, this *is* the rate: extraction is charged per candidate message, so
#: a pass that swallowed a year of history in one go would spend a month's
#: budget in a minute and starve the live pass of the same endpoint while it
#: did. Draining slowly is fine -- history is not going anywhere.
BACKLOG_MESSAGES_PER_PASS = 100

#: How far the backlog is counted for the health endpoint. A figure, not a
#: total: "1000+" says "still draining" as usefully as the true number, and
#: the true number is a scan of every pending row in the corpus.
BACKLOG_COUNT_CAP = 1_000


@dataclass(frozen=True, slots=True)
class BacklogProgress:
    """What the backlog pass has done, and how much of it is left.

    `pending` is the half that makes a stall legible. Counters that only ever
    rise say the pass ran; they cannot say whether it is keeping up, and a
    backlog that stops moving while the process reports itself healthy is the
    exact shape of every silent failure this project has had.
    """

    pending: int = 0
    pending_capped: bool = False
    batches: int = 0
    batches_failed: int = 0
    messages: int = 0
    recorded: int = 0
    last_pass_at: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "pending": self.pending,
            "pending_capped": self.pending_capped,
            "batches": self.batches,
            "batches_failed": self.batches_failed,
            "messages": self.messages,
            "recorded": self.recorded,
            "last_pass_at": self.last_pass_at,
        }


class BacklogExtractionWorker:
    """Extracts asks from the history the gateway never streamed to us.

    `ExtractionWorker` sees a message only if capture submits it, and capture
    submits only what arrives live. Everything backfill imports -- which is
    most of what a channel contains -- was offered to nothing, so "what did
    people ask me to do?" knew only what happened while this process was
    running. That is not what the feature promises.

    So the corpus itself is the queue. A message is pending until the revision
    that was read is recorded against it, which means the backlog survives a
    restart, drains in the order it becomes answerable, and cannot be
    re-extracted because somebody redeployed. The last few minutes of a
    channel are left out of it: those messages are the live pass's, and
    reading them here as well would buy the same model call twice.

    It extracts through the live worker rather than beside it, deliberately:
    the same `ExtractionService`, the same model, the same ask store, the same
    directory of learned names, and the same counters. A second extraction
    path assembled separately is a second set of rules about what an ask is.
    """

    def __init__(
        self,
        worker: ExtractionWorker,
        ledger: ExtractionLedger,
        channels: Sequence[ChannelRef] = (),
        messages_per_pass: int = BACKLOG_MESSAGES_PER_PASS,
        batch_messages: int = WINDOW_MESSAGES,
        count_cap: int = BACKLOG_COUNT_CAP,
    ) -> None:
        self._worker = worker
        self._ledger = ledger
        # Indexing scope, passed in rather than read from a column. The
        # corpus records that a channel was once indexed and nothing ever
        # unsets it, so a channel removed from scope would go on being read
        # and paid for on every pass.
        self._channels = tuple(channels)
        self._per_pass = messages_per_pass
        self._batch = batch_messages
        self._cap = count_cap
        self._progress = BacklogProgress()

    @property
    def progress(self) -> BacklogProgress:
        return self._progress

    async def run_once(self) -> int:
        """Read one bounded slice of the backlog and extract from it.

        Returns the number of messages extracted, so a caller can tell a busy
        pass from an idle one -- though this loop sleeps either way, because
        the interval is half of the rate bound.
        """
        pending = await self._ledger.messages_pending_extraction(
            self._per_pass, self._channels
        )
        if not pending:
            # What this does forever on a server that stopped talking months
            # ago: one index scan over a partial index that finds nothing.
            self._progress = replace(
                self._progress, pending=0, pending_capped=False, last_pass_at=time.time()
            )
            return 0

        done = 0
        for entries in _by_channel(pending):
            done += await self._extract_channel(entries)
        await self._measure_backlog()
        self._progress = replace(self._progress, last_pass_at=time.time())
        return done

    async def _extract_channel(self, entries: Sequence[PendingExtraction]) -> int:
        """Extract one channel's slice, oldest first, in window-sized batches."""
        ordered = sorted(entries, key=_said_at)
        # Everything read this pass can be a reply parent for everything else
        # in it, which is how a backfilled "can you handle it" that replies to
        # a message three batches earlier still resolves to the person it was
        # said to.
        parents = {e.message.platform_message_id: e.message for e in ordered}

        done = 0
        for start in range(0, len(ordered), self._batch):
            chunk = ordered[start : start + self._batch]
            done += await self._extract_chunk(chunk, parents)
        return done

    async def _extract_chunk(
        self, chunk: Sequence[PendingExtraction], parents: Mapping[int, Message]
    ) -> int:
        if not await self._worker.extract_batch([e.message for e in chunk], parents):
            # Left unrecorded on purpose. An unrecorded message is one the next
            # pass reads again, so a batch that failed on a transient model or
            # store error costs a retry rather than a hole in the obligations
            # nobody would ever see.
            self._progress = replace(
                self._progress, batches_failed=self._progress.batches_failed + 1
            )
            return 0

        recorded = await self._record(chunk)
        self._progress = replace(
            self._progress,
            batches=self._progress.batches + 1,
            messages=self._progress.messages + len(chunk),
            recorded=self._progress.recorded + recorded,
        )
        return len(chunk)

    async def _record(self, chunk: Sequence[PendingExtraction]) -> int:
        """Mark the chunk read, at the revisions that were read.

        Absorbed rather than raised for the same reason a failed batch is: a
        store that cannot take the mark costs a repeated extraction later,
        while raising here would end this pass and every channel left in it.
        """
        try:
            return await self._ledger.record_extraction(chunk)
        except Exception:
            log.exception("asks.backlog_record_failed", count=len(chunk))
            return 0

    async def _measure_backlog(self) -> None:
        try:
            depth = await self._ledger.pending_extraction_count(self._cap, self._channels)
        except Exception:
            # A health figure, so its failure must not cost the work that was
            # actually done in this pass.
            log.exception("asks.backlog_count_failed")
            return
        self._progress = replace(
            self._progress, pending=depth, pending_capped=depth >= self._cap
        )


def _said_at(entry: PendingExtraction) -> tuple[datetime, int]:
    return (entry.message.created_at, entry.message.platform_message_id)


def _by_channel(
    pending: Sequence[PendingExtraction],
) -> list[Sequence[PendingExtraction]]:
    """One group per channel, because a window is a conversation in one room."""
    grouped: dict[ChannelRef, list[PendingExtraction]] = {}
    for entry in pending:
        grouped.setdefault(entry.message.channel, []).append(entry)
    return list(grouped.values())
