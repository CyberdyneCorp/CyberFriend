"""The composition root: what it refuses, and what it actually builds.

Two things are being proved here. That the deployment fails at boot rather
than on a request -- an incapable model and a mismatched embedding width both
stop the process, naming what is wrong. And that the graph the bot runs is
the real answer service over a viewer-scoped corpus, which is checked by
asking it a question through the same function `bot.main` calls.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chatmemory.adapters.discord.acl import static_guild
from chatmemory.app.ask import AskRequest, AskService
from chatmemory.app.reasoning.capabilities import (
    MissingCapabilityError,
    ModelCapability,
    Stage,
)
from chatmemory.app.reasoning.errors import ConfigurationError, RetrievalUnavailable
from chatmemory.app.reasoning.ports import JsonCompletion, TextCompletion
from chatmemory.app.reasoning.retrieval import CorpusRetrieval, discord_urls
from chatmemory.app.reasoning.service import ReasoningAnswerService
from chatmemory.composition import (
    ANSWERING_STAGES,
    build_answer_stack,
    build_answers,
    build_ask_service,
    build_chat_model,
    declared_capabilities,
    verify_embedding_width,
)
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

GUILD = 7
GENERAL, LEADERSHIP = 100, 300
LEAD, STAFF = 1, 3
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

BASE = {
    "discord_token": "zzz-discord-bot-token-zzz",
    "discord_guild_id": GUILD,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
    "indexed_channel_ids": f"{GENERAL} {LEADERSHIP}",
}


def settings(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


def ch(channel_id: int) -> ChannelRef:
    return ChannelRef("discord", channel_id)


def hit(window_id: int, channel: int, text: str) -> SearchHit:
    return SearchHit(
        window_id=window_id,
        channel=ch(channel),
        text=text,
        starts_at=NOW,
        ends_at=NOW,
        score=0.016,
        relevance_source=RelevanceSource.FUSED_RRF,
        message_ids=(window_id * 10,),
    )


class FakeSearch:
    """A `SearchBackend` that filters the way the real store's WHERE clause does.

    Filtering here rather than in the assertions is what lets a test claim
    that audience-scoped retrieval never *saw* the restricted channel, rather
    than that something downstream dropped it.
    """

    def __init__(self, *hits: SearchHit, fail: bool = False) -> None:
        self._hits = hits
        self._fail = fail
        self.viewers: list[Viewer] = []

    async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
        if self._fail:
            raise TimeoutError("the database did not answer")
        self.viewers.append(viewer)
        return [h for h in self._hits if h.channel in viewer.visible_channels]

    async def thread_context(
        self, viewer: Viewer, platform_message_id: int, radius: int = 10
    ) -> Sequence[Message]:
        return []

    async def list_channels(self, viewer: Viewer) -> Sequence[ChannelRef]:
        return []


class FakeChat:
    """A model that finds everything sufficient and cites what it was shown.

    It reads the window ids out of the fenced prompt rather than returning a
    constant, so an answer it grounds is grounded in evidence the run
    actually retrieved -- which is the property the citation resolver checks.
    """

    def __init__(self) -> None:
        self.schemas: list[str] = []
        self.prompts: list[str] = []

    async def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], schema_name: str
    ) -> JsonCompletion:
        self.schemas.append(schema_name)
        self.prompts.append(user)
        if schema_name == "evidence_verdict":
            return JsonCompletion(
                {"verdict": "sufficient", "score": 0.9, "suggested_query": None}
            )
        if schema_name == "question_plan":
            return JsonCompletion({"sub_questions": ["what happened with the deploy"]})
        shown = [int(w) for w in re.findall(r"window_id=(\d+)", user)]
        return JsonCompletion(
            {"text": "The deploy was rolled back at noon.", "cited_window_ids": shown}
        )

    async def complete_text(self, system: str, user: str) -> TextCompletion:
        return TextCompletion(text="")


class FakeEmbeddings:
    def __init__(self, width: int, vectors: int = 1) -> None:
        self._width = width
        self._vectors = vectors

    @property
    def dimensions(self) -> int:
        return self._width

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0] * self._width for _ in range(self._vectors)]


def guild() -> FakeGuild:
    return FakeGuild(
        id=GUILD,
        members=[
            FakeMember(LEAD, frozenset({"lead"})),
            FakeMember(STAFF, frozenset()),
        ],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(LEADERSHIP, allowed_roles=frozenset({"lead"})),
        ],
    )


# --- refusing to start --------------------------------------------------


def test_an_incapable_model_refuses_to_start_naming_what_is_missing() -> None:
    """The message has to name the capability: "unsupported model" sends an
    operator to the wrong place, and the wrong place is a live endpoint."""
    with pytest.raises(MissingCapabilityError) as raised:
        build_chat_model(settings(chat_model="some-self-hosted-build"))

    message = str(raised.value)
    assert "structured_output" in message
    assert "some-self-hosted-build" in message
    assert "synthesize" in message


def test_a_deployment_may_declare_what_its_endpoint_provides() -> None:
    chat = build_chat_model(
        settings(
            chat_model="some-self-hosted-build",
            chat_model_capabilities="chat structured_output",
        )
    )
    assert chat.model == "some-self-hosted-build"
    assert chat.provides == {ModelCapability.CHAT, ModelCapability.STRUCTURED_OUTPUT}


def test_a_declaration_missing_a_needed_capability_still_refuses() -> None:
    """Declaring capabilities is not a way to skip the check."""
    with pytest.raises(MissingCapabilityError, match="structured_output"):
        build_chat_model(
            settings(chat_model="gpt-4o", chat_model_capabilities="chat")
        )


def test_a_misspelled_capability_is_a_boot_failure_not_a_narrower_set() -> None:
    with pytest.raises(ConfigurationError) as raised:
        declared_capabilities(
            settings(chat_model_capabilities="chat structured-output")
        )
    assert "structured-output" in str(raised.value)
    assert "structured_output" in str(raised.value)


def test_the_planning_stage_is_validated_even_though_routing_is_lexical() -> None:
    """Routing decides per question, so a model that cannot plan would
    otherwise fail on the first question that happens to reach the loop."""
    assert Stage.PLAN in ANSWERING_STAGES
    assert Stage.SYNTHESIZE in ANSWERING_STAGES


async def test_the_stack_refuses_the_model_before_it_reaches_the_network() -> None:
    """Ordering, not just outcome: the capability check is declarative and
    free, so a deployment that cannot serve must never get as far as opening
    a pool or spending a call on the embedding probe."""
    with pytest.raises(MissingCapabilityError):
        await build_answer_stack(settings(chat_model="some-self-hosted-build"))


async def test_an_embedding_model_of_the_wrong_width_refuses_to_start() -> None:
    with pytest.raises(ConfigurationError) as raised:
        await verify_embedding_width(FakeEmbeddings(768), "text-embedding-3-small", 1536)

    message = str(raised.value)
    assert "768" in message and "1536" in message
    assert "text-embedding-3-small" in message
    assert "reindex" in message


async def test_an_endpoint_returning_no_vector_refuses_to_start() -> None:
    with pytest.raises(ConfigurationError, match="no vector"):
        await verify_embedding_width(FakeEmbeddings(1536, vectors=0), "e5", 1536)


async def test_the_configured_width_passes() -> None:
    await verify_embedding_width(FakeEmbeddings(1536), "text-embedding-3-small", 1536)


# --- what the bot actually gets ----------------------------------------


def _bot_source() -> ast.Module:
    import chatmemory.entrypoints.bot as bot_module

    return ast.parse(Path(inspect.getfile(bot_module)).read_text())


def test_the_bot_no_longer_reaches_for_the_stub() -> None:
    """Source-level, because the stub abstains on every question: a bot wired
    to it starts, connects, answers nothing, and looks healthy throughout."""
    names = {
        node.id for node in ast.walk(_bot_source()) if isinstance(node, ast.Name)
    } | {
        alias.name
        for node in ast.walk(_bot_source())
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "StubAnswerService" not in names
    assert {"build_answer_stack", "build_ask_service"} <= names


def test_the_stub_is_still_available_to_tests() -> None:
    from chatmemory.app.ask import StubAnswerService

    assert StubAnswerService is not None


def test_the_composed_answer_service_is_the_reasoning_one() -> None:
    service = build_answers(
        CorpusRetrieval(FakeSearch(), discord_urls(GUILD)), FakeChat()
    )
    assert isinstance(service, ReasoningAnswerService)


def test_retrieval_cannot_be_performed_without_a_viewer() -> None:
    """Structural: an unfiltered read must be unrepresentable, not untested."""
    viewer = inspect.signature(CorpusRetrieval.retrieve).parameters["viewer"]
    assert viewer.default is inspect.Parameter.empty


async def test_a_corpus_that_cannot_be_searched_is_a_failure_not_an_absence() -> None:
    retrieval = CorpusRetrieval(FakeSearch(fail=True), discord_urls(GUILD))
    with pytest.raises(RetrievalUnavailable):
        await retrieval.retrieve(
            Viewer(PersonRef("discord", LEAD), frozenset({ch(GENERAL)})),
            SearchQuery(text="anything"),
        )


# --- end to end through the composed graph ------------------------------


def compose(search: FakeSearch) -> tuple[AskService, FakeChat]:
    chat = FakeChat()
    answers = build_answers(CorpusRetrieval(search, discord_urls(GUILD)), chat)
    return build_ask_service(settings(), static_guild(guild()), answers), chat


async def test_an_ask_through_the_composed_graph_is_grounded() -> None:
    search = FakeSearch(hit(1, GENERAL, "we rolled the deploy back at noon"))
    asks, chat = compose(search)

    outcome = await asks.ask(
        AskRequest(PersonRef("discord", LEAD), "what happened with the deploy",
                   ch(GENERAL), location_id=GENERAL)
    )

    assert outcome.scoped is not None
    answer = outcome.scoped.answer
    assert not answer.abstained
    assert answer.text == "The deploy was rolled back at noon."
    assert [c.channel for c in answer.citations] == [ch(GENERAL)]
    assert answer.citations[0].url == f"https://discord.com/channels/{GUILD}/{GENERAL}/10"
    # The model was asked to judge and to write: this is the real service.
    assert "evidence_verdict" in chat.schemas and "grounded_answer" in chat.schemas


async def test_retrieval_is_scoped_to_the_audience_not_to_the_asker() -> None:
    """The lead may read #leadership; the room they asked in may not.

    The restricted window must never be retrieved at all -- the delivery
    guard dropping it afterwards would mean the prose was already written
    from it.
    """
    search = FakeSearch(
        hit(1, GENERAL, "we rolled the deploy back at noon"),
        hit(2, LEADERSHIP, "the rollback was because of the incident"),
    )
    asks, _ = compose(search)

    outcome = await asks.ask(
        AskRequest(PersonRef("discord", LEAD), "what happened with the deploy",
                   ch(GENERAL), location_id=GENERAL)
    )

    assert search.viewers, "retrieval never ran"
    for viewer in search.viewers:
        assert viewer.visible_channels == {ch(GENERAL)}
    assert outcome.scoped is not None
    assert outcome.scoped.answer.source_channels == {ch(GENERAL)}
    # Nothing was dropped downstream, so nothing was withheld to notify about.
    assert not outcome.scoped.should_notify_asker


async def test_the_same_question_in_a_direct_message_keeps_the_askers_access() -> None:
    search = FakeSearch(
        hit(1, GENERAL, "we rolled the deploy back at noon"),
        hit(2, LEADERSHIP, "the rollback was because of the incident"),
    )
    asks, _ = compose(search)

    outcome = await asks.ask(
        AskRequest(PersonRef("discord", LEAD), "what happened with the deploy",
                   None, location_id=LEAD)
    )

    assert outcome.scoped is not None
    assert outcome.scoped.answer.source_channels == {ch(GENERAL), ch(LEADERSHIP)}
