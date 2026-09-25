"""The ask pipeline has to be reachable from the two running processes.

Every piece of `app/asks/` was implemented and tested while `ingest.py` started
six loops that did not include extraction and no question ever reached
`ObligationService`. Unit tests could not see that, because each of them built
its collaborators by hand -- which is exactly how this project has shipped six
green, unreachable features.

So these tests are about the wiring: that a captured message reaches the
extractor, that a stalled extractor is visible instead of silent, that an
obligation question is answered from rows rather than from resemblance, and
that the viewer filter still applies when it is.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import chatmemory
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import AskKind, AskPolicy, to_person
from chatmemory.app.asks.obligations import NOTHING_OUTSTANDING, ObligationService
from chatmemory.app.asks.resolution import ObservedDirectory, StaticDirectory
from chatmemory.app.asks.worker import ExtractionWorker
from chatmemory.app.routing import (
    ObligationIntent,
    Route,
    classify,
    obligation_question,
)
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.ports.answers import Answer, Question
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    CARA,
    OPEN_CHANNEL,
    PRIVATE_CHANNEL,
    T0,
    FakeAskStore,
    StubExtractor,
    ask,
    extracted,
    message,
    viewer,
)

SRC = Path(chatmemory.__file__).parent

NOW = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)


def _source(*parts: str) -> str:
    return (SRC.joinpath(*parts)).read_text()


def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text())
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    ]
    assert found, f"{path.name} has no function {name}"
    return found[0]


def _calls(scope: ast.AST, func: str, keyword: str | None = None) -> bool:
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if called != func:
            continue
        if keyword is None or any(k.arg == keyword for k in node.keywords):
            return True
    return False


# --- which questions are obligation questions ---------------------------


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("what did people ask me today", ObligationIntent.ASKED_OF_ME),
        ("what has anyone asked me this week", ObligationIntent.ASKED_OF_ME),
        ("did sam ask me to do anything yesterday", ObligationIntent.ASKED_OF_ME),
        ("what do I need to do today", ObligationIntent.MY_OBLIGATIONS),
        ("what's on my plate", ObligationIntent.MY_OBLIGATIONS),
        ("anything outstanding for me", ObligationIntent.MY_OBLIGATIONS),
        ("what did I promise last week", ObligationIntent.MY_OBLIGATIONS),
        ("is anyone waiting on me", ObligationIntent.MY_OBLIGATIONS),
    ],
)
def test_obligation_questions_are_recognised(text: str, intent: ObligationIntent) -> None:
    asked = obligation_question(text)
    assert asked is not None, text
    assert asked.intent is intent


@pytest.mark.parametrize(
    "text",
    [
        "what happened in #infra yesterday",
        "who deployed the api last week",
        # About somebody else's duties: answering it from the asker's own rows
        # would be wrong, and quietly so.
        "what does sam need to do",
        "what did sam promise about the migration",
        # Two lines of enquiry. An obligation filter answers one thing, so the
        # loop keeps it and can ask this as one of its sub-goals.
        "what did sam ask me and whether I replied",
        "who asked me something? and what did I miss?",
        # Reaching an external system is not a question about `ask` rows.
        "what linear tickets are assigned to me",
    ],
)
def test_everything_else_is_left_to_retrieval(text: str) -> None:
    assert obligation_question(text) is None


def test_the_two_classifiers_stay_independent() -> None:
    """`classify` still answers fixed-or-loop for every question.

    The obligation door is in front of it, not inside it: a caller that asks
    for a route must never get a third answer it has no branch for.
    """
    for text in ("what do I need to do today", "what happened in #infra"):
        assert classify(text).route in (Route.FIXED, Route.LOOP)


def test_the_named_period_is_a_day_not_a_rolling_window() -> None:
    """Somebody asking at 14:00 what was asked of them today is not asking
    about yesterday evening."""
    asked = obligation_question("what did people ask me today")
    assert asked is not None and asked.period is not None
    since, until = asked.period.bounds(NOW)
    assert since == datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    assert until is None


def test_yesterday_is_bounded_at_both_ends() -> None:
    asked = obligation_question("what did people ask me yesterday")
    assert asked is not None and asked.period is not None
    since, until = asked.period.bounds(NOW)
    assert since == datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
    assert until == datetime(2026, 9, 10, 0, 0, tzinfo=UTC)


def test_deciding_costs_no_model_call() -> None:
    asked = obligation_question("what do I need to do")
    assert asked is not None and asked.model_calls == 0


# --- the answer path -----------------------------------------------------


class RecordingAnswers:
    """Stands in for the reasoning service, and counts being reached."""

    def __init__(self) -> None:
        self.questions: list[str] = []

    async def answer(self, question: Question) -> Answer:
        self.questions.append(question.text)
        return Answer(text="from retrieval")


def question(text: str, asker: object, *audience_channels: object) -> Question:
    return Question(
        text=text,
        asker=asker,  # type: ignore[arg-type]
        audience=Audience(
            mode=DeliveryMode.PUBLIC_CHANNEL,
            members=frozenset(),
            readable_channels=frozenset(audience_channels),  # type: ignore[arg-type]
        ),
    )


def service(store: FakeAskStore, fallback: RecordingAnswers) -> object:
    from chatmemory.app.asks.answering import ObligationAnswerService

    return ObligationAnswerService(
        ObligationService(store, AskPolicy(min_confidence=0.6)),
        fallback,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def store_with(*asks: object) -> FakeAskStore:
    store = FakeAskStore()
    for item in asks:
        store.asks[item.key] = item  # type: ignore[attr-defined]
        store.messages[item.source_message_id] = message(  # type: ignore[attr-defined]
            item.source_message_id,  # type: ignore[attr-defined]
            item.requester,  # type: ignore[attr-defined]
            "can you review the migration?",
            channel=item.channel,  # type: ignore[attr-defined]
        )
    return store


async def test_an_obligation_question_never_reaches_similarity_search() -> None:
    """The motivating case. Retrieval must not be consulted at all."""
    store = store_with(ask(source_message_id=10, requester=ALICE, addressee=to_person(BOB)))
    fallback = RecordingAnswers()

    answer = await service(store, fallback).answer(  # type: ignore[attr-defined]
        question("what did people ask me today", viewer(BOB, OPEN_CHANNEL), OPEN_CHANNEL)
    )

    assert fallback.questions == [], "an obligation question was sent to retrieval"
    assert [c.message_id for c in answer.citations] == [10]


async def test_every_other_question_is_passed_through_untouched() -> None:
    fallback = RecordingAnswers()
    answer = await service(FakeAskStore(), fallback).answer(  # type: ignore[attr-defined]
        question("what happened in infra yesterday", viewer(BOB, OPEN_CHANNEL), OPEN_CHANNEL)
    )
    assert fallback.questions == ["what happened in infra yesterday"]
    assert answer.text == "from retrieval"


async def test_nothing_outstanding_is_a_successful_answer() -> None:
    fallback = RecordingAnswers()
    answer = await service(FakeAskStore(), fallback).answer(  # type: ignore[attr-defined]
        question("what do I need to do today", viewer(BOB, OPEN_CHANNEL), OPEN_CHANNEL)
    )
    assert answer.text == NOTHING_OUTSTANDING
    assert not answer.abstained
    assert fallback.questions == []


async def test_asks_from_unreadable_channels_are_not_reported_or_counted() -> None:
    """The viewer filter, through the composed path rather than the store."""
    store = store_with(
        ask(
            source_message_id=10,
            requester=ALICE,
            addressee=to_person(BOB),
            channel=PRIVATE_CHANNEL,
        )
    )
    fallback = RecordingAnswers()
    restricted = viewer(BOB, OPEN_CHANNEL)

    answer = await service(store, fallback).answer(  # type: ignore[attr-defined]
        question("what did people ask me today", restricted, OPEN_CHANNEL)
    )

    assert answer.text == NOTHING_OUTSTANDING
    assert answer.citations == ()
    assert await ObligationService(store).outstanding_count(restricted) == 0


async def test_the_audience_narrows_the_viewer_the_asker_reads_as() -> None:
    """An ask is a summary, and summaries travel. Asking in a public channel
    must not publish an ask from a channel the room cannot read."""
    store = store_with(
        ask(
            source_message_id=10,
            requester=ALICE,
            addressee=to_person(BOB),
            channel=PRIVATE_CHANNEL,
        )
    )
    fallback = RecordingAnswers()
    # Bob can read the private channel; the room he is asking in cannot.
    asked_in_public = question(
        "what did people ask me today", viewer(BOB, OPEN_CHANNEL, PRIVATE_CHANNEL), OPEN_CHANNEL
    )
    in_public = await service(store, fallback).answer(asked_in_public)  # type: ignore[attr-defined]
    assert in_public.text == NOTHING_OUTSTANDING

    asked_privately = question(
        "what did people ask me today",
        viewer(BOB, OPEN_CHANNEL, PRIVATE_CHANNEL),
        OPEN_CHANNEL,
        PRIVATE_CHANNEL,
    )
    in_private = await service(store, fallback).answer(asked_privately)  # type: ignore[attr-defined]
    assert [c.message_id for c in in_private.citations] == [10]


async def test_a_sub_threshold_extraction_is_never_reported_as_an_obligation() -> None:
    store = store_with(
        ask(source_message_id=10, requester=ALICE, addressee=to_person(BOB), confidence=0.2)
    )
    fallback = RecordingAnswers()
    answer = await service(store, fallback).answer(  # type: ignore[attr-defined]
        question("what do I need to do", viewer(BOB, OPEN_CHANNEL), OPEN_CHANNEL)
    )
    assert answer.text == NOTHING_OUTSTANDING


async def test_what_was_asked_of_me_is_filtered_by_the_named_day() -> None:
    store = store_with(
        ask(source_message_id=10, requester=ALICE, addressee=to_person(BOB), asked_at=NOW),
        ask(
            source_message_id=11,
            requester=CARA,
            addressee=to_person(BOB),
            asked_at=T0.replace(year=2026, month=8, day=1),
        ),
    )
    fallback = RecordingAnswers()
    answer = await service(store, fallback).answer(  # type: ignore[attr-defined]
        question("what did people ask me today", viewer(BOB, OPEN_CHANNEL), OPEN_CHANNEL)
    )
    assert [c.message_id for c in answer.citations] == [10]


async def test_what_i_need_to_do_ignores_the_named_period() -> None:
    """A request made last Tuesday and still open is the first thing that
    answer should contain."""
    store = store_with(
        ask(
            source_message_id=10,
            requester=ALICE,
            addressee=to_person(BOB),
            asked_at=T0.replace(month=8, day=1),
        )
    )
    fallback = RecordingAnswers()
    answer = await service(store, fallback).answer(  # type: ignore[attr-defined]
        question("what do I need to do today", viewer(BOB, OPEN_CHANNEL), OPEN_CHANNEL)
    )
    assert [c.message_id for c in answer.citations] == [10]


# --- the extraction pass -------------------------------------------------


def worker_over(
    store: FakeAskStore,
    extractor: StubExtractor,
    directory: ObservedDirectory | None = None,
    **kwargs: object,
) -> ExtractionWorker:
    learner = directory or ObservedDirectory()
    return ExtractionWorker(
        ExtractionService(
            extractor=extractor,
            store=store,
            directory=learner if directory is not None else StaticDirectory({}),
        ),
        directory=learner,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_a_submitted_message_reaches_the_extractor() -> None:
    store = FakeAskStore()
    extractor = StubExtractor(extracted())
    worker = worker_over(store, extractor, window_messages=1)

    worker.submit(
        message(10, ALICE, "@bob can you review this?", mentions=frozenset({BOB}))
    )
    assert await worker.run_once() == 1

    assert len(extractor.calls) == 1
    assert len(store.asks) == 1


async def test_only_messages_with_a_plausible_addressee_are_paid_for() -> None:
    store = FakeAskStore()
    extractor = StubExtractor(extracted())
    worker = worker_over(store, extractor, window_messages=2)

    worker.submit(message(10, ALICE, "deploy finished"))
    worker.submit(message(11, ALICE, "can you take a look at the migration"))
    await worker.run_once()

    assert [c.message.platform_message_id for c in extractor.calls] == [11]


async def test_a_batch_that_raises_does_not_end_the_pass() -> None:
    """A worker that dies on one bad batch stops extracting entirely, and the
    only symptom is that obligations quietly stop appearing."""

    class ExplodingStore(FakeAskStore):
        async def record_asks(self, source_message_id: int, asks: Sequence[object]) -> int:
            if source_message_id == 10:
                raise RuntimeError("store unavailable")
            return await super().record_asks(source_message_id, asks)  # type: ignore[arg-type]

    store = ExplodingStore()
    worker = worker_over(store, StubExtractor(extracted()), window_messages=1)

    worker.submit(message(10, ALICE, "can you review this", mentions=frozenset({BOB})))
    await worker.run_once()
    worker.submit(message(11, ALICE, "can you review this too", mentions=frozenset({BOB})))
    await worker.run_once()

    assert worker.progress.batches_failed == 1
    assert len(store.asks) == 1, "extraction stopped after one bad batch"


async def test_an_unreadable_message_costs_one_extraction_not_the_pass() -> None:
    store = FakeAskStore()
    worker = worker_over(store, StubExtractor(extracted(), fail=True), window_messages=1)

    worker.submit(message(10, ALICE, "can you review this", mentions=frozenset({BOB})))
    await worker.run_once()

    assert worker.progress.failed == 1
    assert worker.progress.batches_failed == 0


async def test_a_partial_batch_is_extracted_once_the_channel_goes_quiet() -> None:
    """Otherwise the last few messages of every conversation sit in memory
    until the next one starts."""
    store = FakeAskStore()
    extractor = StubExtractor(extracted())
    worker = worker_over(store, extractor, window_messages=50, flush_after_seconds=30.0)

    worker.submit(message(10, ALICE, "can you review this", mentions=frozenset({BOB})))
    assert await worker.run_once(now=0.0) == 0
    assert await worker.run_once(now=31.0) == 1
    assert len(extractor.calls) == 1


async def test_a_full_queue_costs_asks_and_never_ingestion() -> None:
    store = FakeAskStore()
    worker = worker_over(store, StubExtractor(extracted()), max_queued=1)

    assert worker.submit(message(10, ALICE, "one")) is True
    assert worker.submit(message(11, ALICE, "two")) is False
    assert worker.progress.dropped == 1


async def test_progress_is_reportable_and_shows_a_stall() -> None:
    store = FakeAskStore()
    worker = worker_over(store, StubExtractor(extracted()), window_messages=1)

    assert worker.progress.last_flush_at is None
    worker.submit(message(10, ALICE, "can you review this", mentions=frozenset({BOB})))
    assert worker.progress.queued == 1

    await worker.run_once()
    reported = worker.progress.as_dict()
    assert reported["queued"] == 0
    assert reported["candidates"] == 1
    assert reported["recorded"] == 1
    assert reported["last_flush_at"] is not None


async def test_a_reply_resolves_to_the_person_replied_to_across_batches() -> None:
    """The carried tail is why: the parent was extracted in an earlier batch."""
    store = FakeAskStore()
    worker = worker_over(store, StubExtractor(extracted()), window_messages=1)

    worker.submit(message(10, CARA, "you should take a look at the migration"))
    await worker.run_once()
    worker.submit(message(11, ALICE, "can you handle it", reply_to_id=10))
    await worker.run_once()

    recorded = [a for a in store.asks.values() if a.source_message_id == 11]
    assert [a.addressee.person for a in recorded] == [CARA]


async def test_names_are_learned_from_the_conversation() -> None:
    """Without this the ingest process resolves every prose name to nobody."""
    store = FakeAskStore()
    directory = ObservedDirectory()
    worker = worker_over(
        store,
        StubExtractor(extracted(addressee_hint="cara")),
        directory=directory,
        window_messages=1,
    )

    # `author_display` is what the platform said about the author at the time,
    # and it is the only roster the ingest process has.
    worker.submit(replace(message(9, CARA, "morning all"), author_display="Cara"))
    await worker.run_once()
    # A candidate by second-person address, naming somebody in prose: no
    # mention, no reply, and the only route to an addressee is the name.
    worker.submit(message(10, ALICE, "could you get cara to review the migration"))
    await worker.run_once()

    recorded = [a for a in store.asks.values() if a.source_message_id == 10]
    assert [a.addressee.person for a in recorded] == [CARA]


def test_a_name_two_people_answer_to_resolves_to_neither() -> None:
    directory = ObservedDirectory()
    directory.learn("alex", ALICE)
    directory.learn("alex", BOB)
    assert directory.resolve("alex") is None


async def test_a_commitment_is_owed_by_the_person_who_made_it() -> None:
    store = FakeAskStore()
    worker = worker_over(
        store, StubExtractor(extracted(kind=AskKind.COMMITMENT)), window_messages=1
    )

    worker.submit(message(10, ALICE, "i'll push the fix tonight"))
    await worker.run_once()

    recorded = list(store.asks.values())
    assert [a.addressee.person for a in recorded] == [ALICE]
    assert [a.kind for a in recorded] == [AskKind.COMMITMENT]


# --- the running processes actually start it ----------------------------


def test_the_ingest_entrypoint_starts_the_extraction_pass() -> None:
    """Source-level, because the failure is silence: an ingest process with no
    extraction loop writes no ask row and looks healthy for ever."""
    source = _source("entrypoints", "ingest.py")
    for job in ("extraction_loop(", "ask_state_loop(", "build_ask_pipeline("):
        assert job in source, f"the ingest process never starts {job}"


def test_the_live_loop_hands_captured_messages_to_the_extractor() -> None:
    live = _function(SRC / "entrypoints" / "ingest.py", "live_loop")
    assert _calls(live, "submit"), (
        "nothing feeds the extraction worker, so it drains an empty queue for ever"
    )


def test_extraction_is_not_conditional_on_a_runtime_probe() -> None:
    """The reconciliation defect, restated: a capability that disappears
    because a `getattr` returned None is one nobody finds again."""
    source = _source("entrypoints", "ingest.py")
    for probe in ('getattr(store, "record_asks"', '"extract_window"'):
        assert probe not in source


def test_the_extraction_loop_reports_its_progress_to_the_health_state() -> None:
    loop = _function(SRC / "entrypoints" / "ingest.py", "extraction_loop")
    assigned = {
        node.slice.value
        for node in ast.walk(loop)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
    }
    assert "asks_extraction" in assigned, "a stalled extractor would be invisible"


def test_the_extraction_loop_survives_its_own_failures() -> None:
    loop = _function(SRC / "entrypoints" / "ingest.py", "extraction_loop")
    assert any(isinstance(node, ast.ExceptHandler) for node in ast.walk(loop))


def test_the_answer_stack_puts_obligations_in_front_of_retrieval() -> None:
    stack = _function(SRC / "composition.py", "build_answer_stack")
    assert _calls(stack, "build_obligations"), "nothing composes the obligation reader"
    assert _calls(stack, "ObligationAnswerService"), (
        "the bot is handed the reasoning service directly, so obligation "
        "questions are answered by similarity search"
    )


def test_the_answer_stack_puts_decisions_behind_obligations() -> None:
    """Obligation -> decision -> reasoning: the decision path is what the
    obligation path hands everything it does not claim to."""
    stack = _function(SRC / "composition.py", "build_answer_stack")
    for node in ast.walk(stack):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == (
            "ObligationAnswerService"
        ):
            fallback = node.args[1]
            assert isinstance(fallback, ast.Call)
            assert getattr(fallback.func, "id", None) == "build_decision_answers"
            assert _calls(fallback, "build_decision_answers", keyword="clock")
            return
    pytest.fail("build_answer_stack never constructs an ObligationAnswerService")


def test_obligation_periods_are_measured_on_the_graphs_clock() -> None:
    """"This week" is bounded by `edges.clock`, like every other route.

    Left to its wall-clock default, an obligation question on the end-to-end
    harness's fixed clock asked about a different week from the one the
    scenario was set in.
    """
    stack = _function(SRC / "composition.py", "build_answer_stack")
    assert _calls(stack, "ObligationAnswerService", keyword="clock")


def test_the_bot_receives_the_obligation_aware_service() -> None:
    """`bot.py` hands `stack.answers` to `build_ask_service`; that field is
    what has to be the obligation-aware one."""
    assert "stack.answers" in _source("entrypoints", "bot.py")
    built = _function(SRC / "composition.py", "build_answer_stack")
    for node in ast.walk(built):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "AnswerStack":
            answers = next(k.value for k in node.keywords if k.arg == "answers")
            # Reachable, not necessarily outermost: the self-description layer
            # wraps it so "what can you do" is answered before anything can
            # search the corpus for it. What matters is that the bot's answer
            # service CONTAINS the obligation-aware one.
            constructed = {
                getattr(n.func, "id", None)
                for n in ast.walk(answers)
                if isinstance(n, ast.Call)
            }
            assert "ObligationAnswerService" in constructed
            assert getattr(answers.func, "id", None) == "SelfDescriptionAnswerService", (
                "self-description must be outermost, or a capability question "
                "reaches retrieval first"
            )
            return
    pytest.fail("build_answer_stack never constructs an AnswerStack")


def test_the_extraction_model_is_the_cheap_one() -> None:
    """Extraction runs over traffic rather than over questions."""
    pipeline = _function(SRC / "composition.py", "build_ask_pipeline")
    used = {
        node.attr
        for node in ast.walk(pipeline)
        if isinstance(node, ast.Attribute) and node.attr.endswith("_model")
    }
    assert used == {"extraction_model"}, used


class _EmptyExtractor:
    """A scripted extractor that is falsy, as anything with `__len__` can be."""

    def __len__(self) -> int:
        return 0

    async def extract(self, candidate: object) -> list[object]:
        return []


def test_an_injected_extractor_is_kept_even_when_falsy() -> None:
    """The e2e seam replaces only the extractor; a truthiness fallback would
    swap a falsy fake for the live OpenAI one behind the test's back."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from chatmemory.composition import build_ask_pipeline
    from chatmemory.config import Settings

    settings = Settings(  # type: ignore[call-arg]
        discord_token="t",
        discord_guild_id=1,
        database_url="postgresql+asyncpg://u:p@h/d",
        llm_api_key="k",
    )
    extractor = _EmptyExtractor()
    engine = create_async_engine("postgresql+asyncpg://u:p@h/d")
    pipeline = build_ask_pipeline(settings, engine, extractor=extractor)  # type: ignore[arg-type]
    assert pipeline.worker._extraction._extractor is extractor


def test_ingest_gives_the_service_the_decision_log() -> None:
    """A deletion is a tombstone, which no cascade sees; without `decisions=`
    the retracted words stay restated in the decision log."""
    main = _function(SRC / "entrypoints" / "ingest.py", "main")
    assert _calls(main, "IngestService", keyword="decisions")


def test_the_obligation_service_cannot_be_asked_without_a_viewer() -> None:
    for method in ("asked_of_me", "what_i_need_to_do", "outstanding_count"):
        parameter = inspect.signature(getattr(ObligationService, method)).parameters["viewer"]
        assert parameter.default is inspect.Parameter.empty


def test_the_worker_never_blocks_the_live_loop() -> None:
    """`submit` is synchronous by design: the corpus is the thing that cannot
    be rebuilt, so capture must never wait on extraction."""
    assert not asyncio.iscoroutinefunction(ExtractionWorker.submit)


# --- the last mile: through the Discord-facing use case ------------------
#
# The answer still has to survive delivery. `enforce_audience` re-checks every
# citation immediately before it reaches Discord and suppresses the whole
# answer if one came from a channel the room cannot read -- so an obligation
# answer that scoped itself correctly must pass through it untouched, and one
# that did not must be caught here rather than in a channel.

GENERAL, LEADERSHIP = 100, 300
LEAD, STAFF = 1, 3


def _ask_service(store: FakeAskStore) -> tuple[object, RecordingAnswers]:
    from chatmemory.adapters.discord.acl import DiscordAclResolver, DiscordAudienceResolver
    from chatmemory.app.ask import AskService
    from chatmemory.app.asks.answering import ObligationAnswerService
    from chatmemory.app.limits import RateLimiter
    from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

    guild = FakeGuild(
        # Two members, and the second one is load-bearing: the audience of a
        # reply in #general is the intersection over everybody who can read
        # #general, and a guild of one lead would make that the lead's own
        # visibility -- which is exactly the conflation this guards against.
        members=[
            FakeMember(LEAD, frozenset({"lead"})),
            FakeMember(STAFF, frozenset()),
        ],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(LEADERSHIP, allowed_roles=frozenset({"lead"})),
        ],
    )
    indexed = (GENERAL, LEADERSHIP)
    retrieval = RecordingAnswers()
    return (
        AskService(
            acl=DiscordAclResolver(guild, indexed),
            audiences=DiscordAudienceResolver(guild, indexed),
            answers=ObligationAnswerService(  # type: ignore[arg-type]
                ObligationService(store), retrieval, clock=lambda: NOW
            ),
            limiter=RateLimiter(),
        ),
        retrieval,
    )


def _private_ask() -> object:
    from chatmemory.domain.identity import ChannelRef, PersonRef

    return ask(
        source_message_id=20,
        requester=PersonRef("discord", 9),
        addressee=to_person(PersonRef("discord", LEAD)),
        channel=ChannelRef("discord", LEADERSHIP),
        asked_at=NOW,
    )


async def test_an_obligation_answered_in_public_leaks_no_private_ask() -> None:
    from chatmemory.app.ask import AskRequest
    from chatmemory.app.disclosure import SUPPRESSED_ANSWER
    from chatmemory.domain.identity import ChannelRef, PersonRef

    store = store_with(_private_ask())
    asks, retrieval = _ask_service(store)

    outcome = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(
            PersonRef("discord", LEAD),
            "what did people ask me today",
            ChannelRef("discord", GENERAL),
            location_id=GENERAL,
        )
    )

    assert outcome.scoped is not None
    assert retrieval.questions == []
    assert outcome.scoped.answer.text == NOTHING_OUTSTANDING
    # Not the suppression message: nothing had to be dropped at delivery,
    # because the ask was never gathered in the first place.
    assert outcome.scoped.answer.text != SUPPRESSED_ANSWER
    assert outcome.scoped.answer.citations == ()


async def test_the_same_question_asked_privately_answers_in_full() -> None:
    from chatmemory.app.ask import AskRequest
    from chatmemory.domain.identity import PersonRef

    store = store_with(_private_ask())
    asks, _ = _ask_service(store)

    outcome = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(
            PersonRef("discord", LEAD), "what did people ask me today", None, location_id=LEAD
        )
    )

    assert outcome.scoped is not None
    assert [c.message_id for c in outcome.scoped.answer.citations] == [20]
