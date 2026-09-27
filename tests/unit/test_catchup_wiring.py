"""Catch-up in the running process, not merely in the test suite.

This project's recurring failure is a feature that is built, tested, merged
and wired to nothing. So this file reads the call chain from
`entrypoints/bot.py` down -- `main` -> `build_catch_up` -> `build_bot` ->
`build_ask_service` -> `AskService(catchup=...)` -- and then drives the object
`build_bot` actually returns, so that "wired" means the summariser answers a
question asked through the surface the process runs.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from chatmemory.app.ask import AskRequest
from chatmemory.app.catchup import (
    CHANNEL_UNAVAILABLE,
    QUIET_PERIOD,
    CatchUpService,
)
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.ports import Grounded, PromptContext, RetrievalResult
from chatmemory.composition import build_ask_service, build_catch_up
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import RelevanceSource, SearchQuery
from chatmemory.entrypoints.bot import build_bot
from chatmemory.ports.answers import Answer, Question
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"

GENERAL, LEADERSHIP = 100, 300
LEAD, STAFF = 1, 3
NOW = datetime(2026, 9, 17, 14, 30, tzinfo=UTC)

BASE = {
    "discord_token": "t",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
    "indexed_channel_ids": "100,300",
}


def ch(cid: int) -> ChannelRef:
    return ChannelRef("discord", cid)


def person(uid: int) -> PersonRef:
    return PersonRef("discord", uid)


def guild() -> FakeGuild:
    return FakeGuild(
        members=[FakeMember(LEAD, frozenset({"lead"})), FakeMember(STAFF, frozenset())],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(LEADERSHIP, allowed_roles=frozenset({"lead"})),
        ],
    )


# --- the chain from the entrypoint ----------------------------------------


def _function_calls(path: Path, function: str) -> set[str]:
    tree = ast.parse(path.read_text())
    [node] = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == function
    ]
    return {
        getattr(c.func, "id", None) or getattr(c.func, "attr", "")
        for c in ast.walk(node)
        if isinstance(c, ast.Call)
    }


def _calls_with_keyword(path: Path, func: str, keyword: str) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == func and any(k.arg == keyword for k in node.keywords):
            return True
    return False


def test_the_bot_process_builds_the_summariser_and_hands_it_down() -> None:
    """assemble -> build_catch_up -> build_bot -> build_ask_service -> AskService."""
    bot = SRC / "entrypoints" / "bot.py"
    assert "build_catch_up" in _function_calls(bot, "assemble"), (
        "assemble() must build the catch-up summariser"
    )
    assert _calls_with_keyword(bot, "build_bot", "catchup"), (
        "assemble() must give build_bot the summariser"
    )
    assert _calls_with_keyword(bot, "build_ask_service", "catchup"), (
        "build_bot must pass it on to the ask service"
    )
    assert _calls_with_keyword(SRC / "composition.py", "AskService", "catchup"), (
        "build_ask_service must give it to AskService"
    )


def test_the_composed_summariser_reaches_the_corpus_through_the_search_backend() -> None:
    """`build_catch_up` takes the backend the answer stack holds, not a new one."""
    built = build_catch_up(Settings(**BASE), object(), object())  # type: ignore[arg-type]
    assert isinstance(built, CatchUpService)


# --- the object the process runs ------------------------------------------


class Retrieval:
    """Records the viewer, and returns one window from #general."""

    def __init__(self, *items: Evidence) -> None:
        self.items = items
        self.viewers: list[Viewer] = []

    async def retrieve(self, viewer: Viewer, query: SearchQuery) -> RetrievalResult:
        self.viewers.append(viewer)
        return RetrievalResult(
            items=tuple(i for i in self.items if i.channel in viewer.visible_channels)
        )


class Synthesizer:
    async def synthesize(
        self, question: str, evidence: Sequence[Evidence], context: PromptContext
    ) -> Grounded:
        return Grounded(
            text="They shipped pricing.",
            cited_window_ids=tuple(e.window_id for e in evidence),
        )


class Answers:
    """The ordinary answer service. A catch-up must never reach it."""

    def __init__(self) -> None:
        self.seen: list[Question] = []

    async def answer(self, question: Question) -> Answer:
        self.seen.append(question)
        return Answer("searched the corpus", consulted_channels=frozenset())


def window(channel: int, window_id: int, text: str) -> Evidence:
    return Evidence(
        window_id=window_id,
        channel=ch(channel),
        text=text,
        score=0.9,
        relevance_source=RelevanceSource.FUSED_RRF,
        url=f"https://discord.com/channels/1/{channel}/{window_id}",
        author_display="someone",
    )


def bot(*items: Evidence) -> tuple[object, Retrieval, Answers]:
    retrieval = Retrieval(*items)
    answers = Answers()
    graph = build_bot(
        Settings(**BASE),  # type: ignore[arg-type]
        answers,  # type: ignore[arg-type]
        catchup=CatchUpService(
            retrieval,  # type: ignore[arg-type]
            Synthesizer(),
            clock=lambda: NOW,
        ),
    )
    fake = guild()
    graph.client.get_guild = lambda _id: fake  # type: ignore[assignment,method-assign,return-value]
    return graph.asks, retrieval, answers


async def test_a_catch_up_asked_through_the_built_bot_is_summarised() -> None:
    """The question the running process would receive, answered by the summariser."""
    asks, retrieval, answers = bot(window(GENERAL, 42, "we shipped the pricing page"))

    outcome = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(LEAD), "what did I miss in <#100>?", ch(GENERAL), GENERAL)
    )

    assert outcome.scoped is not None
    assert "They shipped pricing." in outcome.scoped.answer.text
    assert [c.channel for c in outcome.scoped.answer.citations] == [ch(GENERAL)]
    # The corpus answer service was never asked: this is one answer, not two.
    assert answers.seen == []
    assert retrieval.viewers[0].visible_channels == frozenset({ch(GENERAL)})


async def test_a_catch_up_on_a_channel_the_room_cannot_read_is_refused_there() -> None:
    """The lead may read #leadership; #general may not, so neither may this answer."""
    asks, retrieval, _ = bot(window(LEADERSHIP, 43, "the reorg is on"))

    outcome = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(LEAD), "catch me up on <#300>", ch(GENERAL), GENERAL)
    )

    assert outcome.scoped is not None
    assert outcome.scoped.answer.text == CHANNEL_UNAVAILABLE
    assert outcome.scoped.answer.citations == ()
    assert retrieval.viewers == []


async def test_the_same_refusal_reaches_someone_with_no_access_at_all() -> None:
    """Staff and the lead get the identical sentence in #general.

    If the two differed, the refusal would answer "does #leadership exist"
    for anyone who cared to try ids.
    """
    asks, _, _ = bot(window(LEADERSHIP, 43, "the reorg is on"))

    lead = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(LEAD), "catch me up on <#300>", ch(GENERAL), GENERAL)
    )
    staff = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(STAFF), "catch me up on <#300>", ch(GENERAL), GENERAL)
    )
    assert lead.scoped is not None and staff.scoped is not None
    assert lead.scoped.answer.text == staff.scoped.answer.text == CHANNEL_UNAVAILABLE


async def test_a_private_catch_up_uses_the_askers_own_access() -> None:
    asks, retrieval, _ = bot(window(LEADERSHIP, 43, "the reorg is on"))

    outcome = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(LEAD), "catch me up on <#300>", None, LEAD)
    )

    assert outcome.scoped is not None
    assert "They shipped pricing." in outcome.scoped.answer.text
    assert retrieval.viewers[0].visible_channels == frozenset({ch(LEADERSHIP)})


async def test_a_quiet_channel_is_reported_quiet_through_the_bot() -> None:
    asks, _, _ = bot()  # nothing retrievable

    outcome = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(STAFF), "o que eu perdi em <#100> hoje?", ch(GENERAL), GENERAL)
    )

    assert outcome.scoped is not None
    assert QUIET_PERIOD in outcome.scoped.answer.text


async def test_an_unwired_deployment_answers_the_question_from_the_corpus() -> None:
    """No summariser: the question is searched for, never refused or crashed.

    This is the safe half of the feature, and the half a process that wires
    nothing should get -- under exactly the same access, because that search
    is scoped by the same viewer the summary would have been.
    """
    answers = Answers()
    graph = build_bot(Settings(**BASE), answers)  # type: ignore[arg-type]
    fake = guild()
    graph.client.get_guild = lambda _id: fake  # type: ignore[assignment,method-assign,return-value]

    outcome = await graph.asks.ask(
        AskRequest(person(LEAD), "what did I miss in <#100>?", ch(GENERAL), GENERAL)
    )

    assert outcome.scoped is not None
    assert outcome.scoped.answer.text == "searched the corpus"
    assert [q.text for q in answers.seen] == ["what did I miss in <#100>?"]


async def test_an_ordinary_question_still_reaches_the_answer_service() -> None:
    asks, retrieval, answers = bot(window(GENERAL, 42, "we shipped the pricing page"))

    outcome = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(LEAD), "who owns billing?", ch(GENERAL), GENERAL)
    )

    assert outcome.scoped is not None
    assert outcome.scoped.answer.text == "searched the corpus"
    assert retrieval.viewers == []


def test_build_ask_service_accepts_the_summariser_directly() -> None:
    """The composition seam, exercised without the Discord client."""
    from chatmemory.adapters.discord.acl import static_guild

    asks = build_ask_service(
        Settings(**BASE),  # type: ignore[arg-type]
        static_guild(guild()),
        object(),  # type: ignore[arg-type]
        catchup=CatchUpService(Retrieval(), Synthesizer(), clock=lambda: NOW),
    )
    assert asks is not None


# --- traced like every other answer ----------------------------------------


def test_the_bot_process_hands_the_stacks_tracer_to_the_ask_service() -> None:
    """Catch-up answers before the answer chain and its tracer seam, so the
    stack's tracer must reach AskService or catch-up is never traced."""
    bot = SRC / "entrypoints" / "bot.py"
    assert _calls_with_keyword(bot, "build_bot", "tracer"), (
        "assemble() must give build_bot the answer stack's tracer"
    )
    assert _calls_with_keyword(bot, "build_ask_service", "tracer"), (
        "build_bot must pass it on to the ask service"
    )
    assert _calls_with_keyword(SRC / "composition.py", "AskService", "tracer"), (
        "build_ask_service must give it to AskService"
    )
    assert _calls_with_keyword(SRC / "composition.py", "AnswerStack", "tracer"), (
        "the answer stack must carry the tracer its answer chain exports through"
    )


async def test_a_catch_up_through_the_built_bot_is_traced_as_corpus_catchup() -> None:
    from chatmemory.app.reasoning import features
    from tests.unit.test_run_tracing import CapturingTracer

    tracer = CapturingTracer()
    answers = Answers()
    graph = build_bot(
        Settings(**BASE),  # type: ignore[arg-type]
        answers,  # type: ignore[arg-type]
        catchup=CatchUpService(
            Retrieval(window(GENERAL, 42, "we shipped the pricing page")),  # type: ignore[arg-type]
            Synthesizer(),
            clock=lambda: NOW,
        ),
        tracer=tracer,
    )
    fake = guild()
    graph.client.get_guild = lambda _id: fake  # type: ignore[assignment,method-assign,return-value]

    await graph.asks.ask(
        AskRequest(person(LEAD), "what did I miss in <#100>?", ch(GENERAL), GENERAL)
    )

    [traced] = tracer.traces
    assert traced.record.feature == features.CORPUS_CATCHUP
    assert traced.record.evidence_window_ids == (42,)
    assert [e.window_id for e in traced.evidence] == [42]
    assert answers.seen == []
