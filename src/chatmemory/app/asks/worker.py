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
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

import structlog

from chatmemory.app.asks.extraction import ExtractionReport, ExtractionService
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.messages import Message

log = structlog.get_logger()


class NameLearner(Protocol):
    """Something that learns who people are from what it sees.

    Here so the worker can feed the addressee directory without owning one:
    the names an ask can refer to are the names of the people talking, and the
    only place both the messages and the directory are in scope is here.
    """

    def observe(self, message: Message) -> None: ...

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

        try:
            report = await self._extraction.extract_window(batch, buffer.parents())
        except Exception:
            # One channel's batch must not end the pass. The messages are
            # already persisted, so nothing is lost that a later re-extraction
            # could not recover; what would be lost is every *other* channel's
            # extraction, for good, if this propagated.
            log.exception("asks.extraction_batch_failed", channel=str(channel))
            self._progress = replace(
                self._progress, batches_failed=self._progress.batches_failed + 1
            )
            return 0

        buffer.carried = (buffer.carried + batch)[-CARRIED_MESSAGES:]
        self._record(report)
        return len(batch)

    def _record(self, report: ExtractionReport) -> None:
        current = self._progress
        self._progress = replace(
            current,
            windows=current.windows + 1,
            candidates=current.candidates + report.candidates,
            extracted=current.extracted + report.extracted,
            recorded=current.recorded + report.recorded,
            failed=current.failed + report.failed,
            last_flush_at=time.time(),
        )
