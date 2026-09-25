"""Obligations have to come out of history, not only out of the live stream.

`ExtractionWorker` sees a message only because capture submits it, and capture
submits only what the gateway hands it. Everything backfill imports -- which is
most of what a channel contains -- was offered to nothing, so "what did people
ask me to do?" answered from whatever happened since the last deploy and looked
entirely healthy doing it.

These tests are about the second half: that the corpus itself is the queue,
that a message is read once and not paid for twice, that an edit racing its own
extraction is read again rather than marked done, that one bad batch costs a
retry instead of the pass -- and, at the bottom, that the running process
actually starts the thing.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import chatmemory
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import AskCandidate, Extraction
from chatmemory.app.asks.resolution import ObservedDirectory, StaticDirectory
from chatmemory.app.asks.worker import BacklogExtractionWorker, ExtractionWorker
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message
from chatmemory.ports.store import PendingExtraction, Store
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    CARA,
    PRIVATE_CHANNEL,
    T0,
    FakeAskStore,
    StubExtractor,
    extracted,
    message,
)

SRC = Path(chatmemory.__file__).parent


class FakeCorpus:
    """In-memory `ExtractionLedger` that keeps the rules the SQL keeps.

    Pending is "the revision recorded differs from the revision stored", which
    is exactly the pair of columns migration 0010 adds. `edit` bumps the
    revision the way the upsert's conflict clause does, so a mark naming the
    revision that was read leaves an edited message pending -- the race the
    window watermark's generation counter exists for, one row at a time.
    """

    def __init__(self) -> None:
        self.messages: dict[int, Message] = {}
        self.revision: dict[int, int] = {}
        self.extracted: dict[int, int] = {}
        self.reads = 0
        self.counts = 0
        self.recorded: list[PendingExtraction] = []
        self.fail_records = False

    # --- what ingestion does to it ---------------------------------------

    def imported(self, *messages: Message) -> None:
        """History arriving through backfill: written, submitted to nothing."""
        for item in messages:
            self.messages[item.platform_message_id] = item
            self.revision.setdefault(item.platform_message_id, 0)

    def edit(self, message_id: int, content: str) -> None:
        stored = self.messages[message_id]
        self.messages[message_id] = replace(stored, content=content)
        self.revision[message_id] += 1

    def is_pending(self, message_id: int) -> bool:
        return self.extracted.get(message_id) != self.revision[message_id]

    # --- the port --------------------------------------------------------

    async def messages_pending_extraction(
        self, limit: int, channels: Sequence[ChannelRef] = ()
    ) -> Sequence[PendingExtraction]:
        self.reads += 1
        pending = [
            PendingExtraction(message=item, generation=self.revision[mid])
            for mid, item in self.messages.items()
            if item.is_visible and self.is_pending(mid)
        ]
        pending.sort(key=lambda e: e.message.created_at, reverse=True)
        return pending[:limit]

    async def record_extraction(self, entries: Sequence[PendingExtraction]) -> int:
        if self.fail_records:
            raise RuntimeError("store unavailable")
        for entry in entries:
            mid = entry.message.platform_message_id
            # COALESCE: a caller with no generation records whatever is current.
            self.extracted[mid] = (
                entry.generation if entry.generation is not None else self.revision[mid]
            )
            self.recorded.append(entry)
        return len(entries)

    async def pending_extraction_count(
        self, cap: int = 1000, channels: Sequence[ChannelRef] = ()
    ) -> int:
        self.counts += 1
        return min(sum(1 for mid in self.messages if self.is_pending(mid)), cap)


def backlog(
    corpus: FakeCorpus,
    extractor: StubExtractor,
    store: FakeAskStore | None = None,
    directory: ObservedDirectory | None = None,
    **kwargs: object,
) -> tuple[BacklogExtractionWorker, FakeAskStore]:
    asks = store or FakeAskStore()
    learner = directory or ObservedDirectory()
    worker = ExtractionWorker(
        ExtractionService(
            extractor=extractor,
            store=asks,
            directory=learner if directory is not None else StaticDirectory({}),
        ),
        directory=learner,
    )
    return BacklogExtractionWorker(worker, corpus, **kwargs), asks  # type: ignore[arg-type]


def asked(
    message_id: int,
    author: PersonRef = ALICE,
    content: str = "can you review this?",
    **kwargs: object,
) -> Message:
    """A message carrying a plausible addressee, so it is worth a model call."""
    return message(message_id, author, content, **kwargs)  # type: ignore[arg-type]


# --- the backlog itself --------------------------------------------------


async def test_history_that_was_never_streamed_is_extracted() -> None:
    """The whole point: backfill imported it, so nothing ever offered it."""
    corpus = FakeCorpus()
    corpus.imported(asked(10, ALICE, "@bob can you review this?", mentions=frozenset({BOB})))
    worker, asks = backlog(corpus, StubExtractor(extracted()))

    assert await worker.run_once() == 1
    assert len(asks.asks) == 1


async def test_an_extracted_message_is_not_paid_for_twice() -> None:
    corpus = FakeCorpus()
    corpus.imported(asked(10, ALICE, "can you review this?"))
    extractor = StubExtractor(extracted())
    worker, _ = backlog(corpus, extractor)

    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    assert len(extractor.calls) == 1


async def test_an_idle_pass_costs_one_query_and_no_model_call() -> None:
    """This runs forever on a channel that stopped changing months ago."""
    corpus = FakeCorpus()
    extractor = StubExtractor(extracted())
    worker, _ = backlog(corpus, extractor)

    assert await worker.run_once() == 0
    assert corpus.reads == 1
    assert corpus.counts == 0, "an empty backlog is not worth counting"
    assert extractor.calls == []


async def test_a_message_edited_while_it_was_extracted_is_read_again() -> None:
    """Migration 0009's race, one row at a time.

    The mark names the revision that was read, so an edit landing during the
    model call leaves the message pending instead of being cleared by a mark
    for text nobody extracted.
    """
    corpus = FakeCorpus()
    corpus.imported(asked(10, ALICE, "can you review this?"))

    class EditsWhileExtracting(StubExtractor):
        async def extract(self, candidate: AskCandidate) -> Extraction:
            corpus.edit(10, "can you review this instead?")
            return await super().extract(candidate)

    worker, _ = backlog(corpus, EditsWhileExtracting(extracted()))

    assert await worker.run_once() == 1
    assert corpus.is_pending(10), "the edit was swallowed by the mark that followed it"
    assert await worker.run_once() == 1


async def test_a_batch_that_fails_is_left_for_the_next_pass() -> None:
    """A worker that dies on one bad batch stops extracting entirely, and the
    only symptom is that obligations quietly stop appearing."""

    class ExplodingStore(FakeAskStore):
        async def record_asks(self, source_message_id: int, asks: Sequence[object]) -> int:
            if source_message_id == 10:
                raise RuntimeError("store unavailable")
            return await super().record_asks(source_message_id, asks)  # type: ignore[arg-type]

    corpus = FakeCorpus()
    corpus.imported(
        asked(10, ALICE, "can you review this?"),
        asked(11, ALICE, "can you review this too?", channel=PRIVATE_CHANNEL),
    )
    store = ExplodingStore()
    worker, _ = backlog(corpus, StubExtractor(extracted()), store=store)

    await worker.run_once()

    assert worker.progress.batches_failed == 1
    assert corpus.is_pending(10), "a failed batch was marked as read"
    assert not corpus.is_pending(11), "one bad batch ended the whole pass"
    assert len(store.asks) == 1


async def test_a_mark_that_cannot_be_written_costs_a_repeat_not_the_pass() -> None:
    corpus = FakeCorpus()
    corpus.imported(asked(10, ALICE, "can you review this?"))
    corpus.fail_records = True
    worker, asks = backlog(corpus, StubExtractor(extracted()))

    assert await worker.run_once() == 1
    assert len(asks.asks) == 1
    assert corpus.is_pending(10)


async def test_one_pass_reads_a_bounded_slice_of_history() -> None:
    """Extraction is a model call per candidate, so a pass that read a year of
    archive in one go would spend the budget in a minute."""
    corpus = FakeCorpus()
    corpus.imported(
        *(
            asked(1000 + n, ALICE, "can you review this?", at=T0 + timedelta(minutes=n))
            for n in range(50)
        )
    )
    extractor = StubExtractor(extracted())
    worker, _ = backlog(corpus, extractor, messages_per_pass=10, batch_messages=5)

    assert await worker.run_once() == 10
    assert len(extractor.calls) == 10


async def test_the_newest_history_drains_first() -> None:
    """Recent obligations are the ones somebody still cares about."""
    corpus = FakeCorpus()
    corpus.imported(
        asked(10, ALICE, "can you review the old one?", at=T0),
        asked(11, ALICE, "can you review the new one?", at=T0 + timedelta(days=30)),
    )
    extractor = StubExtractor(extracted())
    worker, _ = backlog(corpus, extractor, messages_per_pass=1)

    await worker.run_once()
    assert [c.message.platform_message_id for c in extractor.calls] == [11]


async def test_each_channel_is_extracted_as_its_own_conversation() -> None:
    """A window is a conversation in one room; mixing two is context that never
    happened."""
    corpus = FakeCorpus()
    corpus.imported(
        asked(10, ALICE, "can you review this?", at=T0),
        asked(11, CARA, "can you deploy it?", at=T0, channel=PRIVATE_CHANNEL),
    )
    extractor = StubExtractor(extracted())
    worker, _ = backlog(corpus, extractor)

    await worker.run_once()

    for call in extractor.calls:
        assert all(c.channel == call.message.channel for c in call.context)


async def test_a_backfilled_mention_still_says_who_the_ask_fell_to() -> None:
    """The live path is handed mentions by the gateway; a message read back out
    of the corpus has to bring its own, or every "@bob can you..." in history
    arrives looking like chatter."""
    corpus = FakeCorpus()
    corpus.imported(asked(10, ALICE, "@bob can you review this?", mentions=frozenset({BOB})))
    worker, asks = backlog(corpus, StubExtractor(extracted()))

    await worker.run_once()

    assert [a.addressee.person for a in asks.asks.values()] == [BOB]


async def test_names_are_learned_from_backfilled_history() -> None:
    """Otherwise every prose name in the archive resolves to nobody."""
    corpus = FakeCorpus()
    directory = ObservedDirectory()
    corpus.imported(
        replace(asked(9, CARA, "morning all", at=T0), author_display="Cara"),
        asked(10, ALICE, "could you get cara to review the migration", at=T0),
    )
    worker, asks = backlog(
        corpus,
        StubExtractor(extracted(addressee_hint="cara")),
        directory=directory,
    )

    await worker.run_once()

    recorded = [a for a in asks.asks.values() if a.source_message_id == 10]
    assert [a.addressee.person for a in recorded] == [CARA]


async def test_progress_says_how_much_is_left() -> None:
    """Counters that only rise say the pass ran; they cannot say it is keeping
    up, and a backlog that stops draining is the shape of every silent failure
    this project has had."""
    corpus = FakeCorpus()
    corpus.imported(
        *(
            asked(1000 + n, ALICE, "can you review this?", at=T0 + timedelta(minutes=n))
            for n in range(5)
        )
    )
    worker, _ = backlog(corpus, StubExtractor(extracted()), messages_per_pass=2)

    await worker.run_once()
    reported = worker.progress.as_dict()

    assert reported["messages"] == 2
    assert reported["recorded"] == 2
    assert reported["pending"] == 3
    assert reported["last_pass_at"] is not None

    await worker.run_once()
    await worker.run_once()
    assert worker.progress.as_dict()["pending"] == 0


async def test_a_deleted_message_is_never_offered_for_extraction() -> None:
    corpus = FakeCorpus()
    corpus.imported(
        replace(asked(10, ALICE, "can you review this?"), deleted_at=T0)
    )
    extractor = StubExtractor(extracted())
    worker, _ = backlog(corpus, extractor)

    assert await worker.run_once() == 0
    assert extractor.calls == []


# --- the live pass marks what it did, so this one does not repeat it ------


async def test_the_live_pass_records_what_it_extracted() -> None:
    """Without this every live message is extracted twice: once from the queue
    and once from the corpus, on two separate bills."""
    corpus = FakeCorpus()
    live = asked(10, ALICE, "can you review this?")
    corpus.imported(live)

    extractor = StubExtractor(extracted())
    worker = ExtractionWorker(
        ExtractionService(
            extractor=extractor, store=FakeAskStore(), directory=StaticDirectory({})
        ),
        window_messages=1,
    )
    worker.records_through(corpus)  # type: ignore[arg-type]

    worker.submit(live)
    await worker.run_once()

    assert not corpus.is_pending(10)
    assert BacklogExtractionWorker(worker, corpus)  # type: ignore[arg-type]
    assert await BacklogExtractionWorker(worker, corpus).run_once() == 0  # type: ignore[arg-type]


async def test_a_worker_with_no_ledger_still_extracts() -> None:
    """The live pass predates the ledger and must not depend on one."""
    store = FakeAskStore()
    worker = ExtractionWorker(
        ExtractionService(
            extractor=StubExtractor(extracted()),
            store=store,
            directory=StaticDirectory({}),
        ),
        window_messages=1,
    )
    worker.submit(asked(10, ALICE, "can you review this?"))
    assert await worker.run_once() == 1
    assert len(store.asks) == 1


# --- the running process actually starts it ------------------------------


def _source(*parts: str) -> str:
    return (SRC.joinpath(*parts)).read_text()


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} has no function {name}")


def _calls(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
        for n in ast.walk(node)
    )


def test_the_ingest_entrypoint_starts_the_backlog_pass() -> None:
    """Source-level, because the failure is silence: a process that never runs
    this extracts nothing from history and reports itself healthy."""
    main = _function(SRC / "entrypoints" / "ingest.py", "main")
    assert _calls(main, "BacklogExtractionWorker"), "nothing builds the backlog worker"
    assert _calls(main, "backlog_extraction_loop"), "the backlog worker is never run"


def test_the_live_pass_is_told_where_to_record_what_it_extracts() -> None:
    main = _function(SRC / "entrypoints" / "ingest.py", "main")
    assert _calls(main, "records_through"), (
        "the live pass records nothing, so the backlog pass pays for every "
        "live message a second time"
    )


def test_the_backlog_loop_reports_its_progress_to_the_health_state() -> None:
    loop = _function(SRC / "entrypoints" / "ingest.py", "backlog_extraction_loop")
    assigned = {
        node.slice.value
        for node in ast.walk(loop)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
    }
    assert "asks_backlog" in assigned, "a stalled backlog would be invisible"


def test_the_backlog_loop_survives_its_own_failures() -> None:
    loop = _function(SRC / "entrypoints" / "ingest.py", "backlog_extraction_loop")
    assert any(isinstance(node, ast.ExceptHandler) for node in ast.walk(loop))


def test_the_backlog_loop_is_rate_bound() -> None:
    """It sleeps after every pass, busy or idle: the interval is half the bound
    and the slice size is the other half."""
    loop = _function(SRC / "entrypoints" / "ingest.py", "backlog_extraction_loop")
    assert _calls(loop, "sleep")
    body = ast.get_source_segment(_source("entrypoints", "ingest.py"), loop) or ""
    assert "if" not in body.split("await asyncio.sleep")[0].split("while True:")[-1], (
        "the sleep is conditional, so a busy pass runs unbounded"
    )


def test_the_backlog_is_not_conditional_on_a_runtime_probe() -> None:
    """The reconciliation defect, restated: a capability that disappears
    because a `getattr` returned None is one nobody finds again."""
    source = _source("entrypoints", "ingest.py")
    assert 'getattr(store, "messages_pending_extraction"' not in source


@pytest.mark.parametrize(
    "name",
    ["messages_pending_extraction", "record_extraction", "pending_extraction_count"],
)
def test_the_store_port_declares_the_backlog_methods(name: str) -> None:
    """Declared on the port, so the type checker proves at the wiring site what
    a runtime probe would discover and discard."""
    assert callable(getattr(Store, name, None))
    assert callable(getattr(PostgresStore, name, None))


def test_the_backlog_query_takes_no_viewer() -> None:
    """It acts for nobody, and binding a viewer would leave the rest of a
    server's obligations permanently unextracted -- the audit in
    test_sql_audit.py is where that is written down."""
    parameters = inspect.signature(PostgresStore.messages_pending_extraction).parameters
    assert "viewer" not in parameters
