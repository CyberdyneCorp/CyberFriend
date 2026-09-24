"""The said-by route in the running process, not merely in the test suite.

Reads the chain from `entrypoints/bot.py` down -- `assemble` ->
`build_said_by` -> `build_bot` -> `build_ask_service` ->
`AskService(said_by=...)` -- and then drives the object `build_bot` returns,
so that "wired" means the route answers a question asked through the surface
the process runs, ahead of the ordinary answer service and behind catch-up.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from chatmemory.app.ask import AskRequest
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.ports import Grounded, PromptContext, RetrievalResult
from chatmemory.app.said_by import SaidByService
from chatmemory.composition import build_said_by
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import PersonCandidate, RelevanceSource, SearchQuery
from chatmemory.entrypoints.bot import build_bot
from chatmemory.ports.answers import Answer, Question
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"

GENERAL, LEADERSHIP = 100, 300
LEAD, STAFF = 1, 3
ANA = PersonCandidate(PersonRef("discord", 21), "Ana")
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


def _calls_with_keyword(path: Path, func: str, keyword: str) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == func and any(k.arg == keyword for k in node.keywords):
            return True
    return False


def test_the_bot_process_builds_the_route_and_hands_it_down() -> None:
    bot = SRC / "entrypoints" / "bot.py"
    assert "build_said_by(" in bot.read_text(), "assemble() must build the route"
    assert _calls_with_keyword(bot, "build_bot", "said_by")
    assert _calls_with_keyword(bot, "build_ask_service", "said_by")
    assert _calls_with_keyword(SRC / "composition.py", "AskService", "said_by")


def test_the_composed_route_reads_the_deployment_timezone() -> None:
    built = build_said_by(Settings(**BASE), object(), object())  # type: ignore[arg-type]
    assert isinstance(built, SaidByService)
    assert built._tz == ZoneInfo("America/Sao_Paulo")


class People:
    def __init__(self) -> None:
        self.viewers: list[Viewer] = []

    async def people_named(
        self, viewer: Viewer, name: str, limit: int = 6
    ) -> Sequence[PersonCandidate]:
        self.viewers.append(viewer)
        return [ANA] if name.casefold() == "ana" else []


class Retrieval:
    def __init__(self) -> None:
        self.queries: list[SearchQuery] = []

    async def retrieve(self, viewer: Viewer, query: SearchQuery) -> RetrievalResult:
        self.queries.append(query)
        item = Evidence(
            window_id=42,
            channel=ch(GENERAL),
            text="Ana: pricing goes up",
            score=0.9,
            relevance_source=RelevanceSource.VECTOR,
            author_display="Ana",
            message_ids=(42,),
        )
        return RetrievalResult(items=(item,))


class Synthesizer:
    async def synthesize(
        self, question: str, evidence: Sequence[Evidence], context: PromptContext
    ) -> Grounded:
        return Grounded(text="Ana said pricing goes up.", cited_window_ids=(42,))


class Answers:
    def __init__(self) -> None:
        self.seen: list[Question] = []

    async def answer(self, question: Question) -> Answer:
        self.seen.append(question)
        return Answer("searched the corpus", consulted_channels=frozenset())


def bot() -> tuple[object, Retrieval, Answers]:
    retrieval, answers = Retrieval(), Answers()
    graph = build_bot(
        Settings(**BASE),  # type: ignore[arg-type]
        answers,  # type: ignore[arg-type]
        said_by=SaidByService(
            retrieval,
            Synthesizer(),
            People(),
            tz=ZoneInfo("America/Sao_Paulo"),
            clock=lambda: NOW,
        ),
    )
    fake = guild()
    graph.client.get_guild = lambda _id: fake  # type: ignore[assignment,method-assign,return-value]
    return graph.asks, retrieval, answers


async def test_a_said_by_question_through_the_built_bot_is_answered_by_the_route() -> None:
    asks, retrieval, answers = bot()

    outcome = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(STAFF), "what did Ana say about pricing?", ch(GENERAL), GENERAL)
    )

    assert outcome.scoped is not None
    assert "Ana said pricing goes up." in outcome.scoped.answer.text
    assert [q.authors for q in retrieval.queries] == [frozenset({ANA.ref})]
    assert answers.seen == [], "one answer, not two"


async def test_an_unknown_name_falls_through_to_the_answer_service() -> None:
    asks, retrieval, answers = bot()

    outcome = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(STAFF), "what did the release notes say about pricing?", None, STAFF)
    )
    unknown = await asks.ask(  # type: ignore[attr-defined]
        AskRequest(person(STAFF), "what did Zed say about pricing?", None, STAFF)
    )

    assert outcome.scoped is not None and unknown.scoped is not None
    assert unknown.scoped.answer.text == "searched the corpus"
    assert len(answers.seen) == 2 and retrieval.queries == []


async def test_catch_up_and_obligation_questions_are_not_captured() -> None:
    asks, retrieval, answers = bot()

    for text in ("o que o João me pediu?", "what did I miss in <#100>?"):
        await asks.ask(  # type: ignore[attr-defined]
            AskRequest(person(STAFF), text, ch(GENERAL), GENERAL)
        )

    assert retrieval.queries == []
    assert len(answers.seen) == 2
