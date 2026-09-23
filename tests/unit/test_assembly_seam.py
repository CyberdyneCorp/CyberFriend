"""The seam between the bot's object graph and the network.

`assemble(settings, edges)` builds everything `main` runs; `Edges` is the only
thing it reaches the outside world through. Two things are proved here: that
production still builds the same edges it always did, and that nothing inside
`assemble` or `build_answer_stack` builds a model, embedding client or engine
of its own -- which is what would let an end-to-end test silently exercise a
different graph. The HTTP transport and the clock are proved to reach their
users in `test_transport_and_clock_seam`.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chatmemory.adapters.llm.chat import OpenAICompatibleChat
from chatmemory.adapters.llm.embeddings import OpenAICompatibleEmbeddings
from chatmemory.app.reasoning.errors import ConfigurationError
from chatmemory.app.reasoning.ports import (
    ChatModel,
    JsonCompletion,
    TextCompletion,
    ToolCompletion,
    ToolDefinition,
)
from chatmemory.composition import Edges, build_answer_stack, utc_now
from chatmemory.config import Settings
from chatmemory.entrypoints.bot import Process, assemble, build_bot
from tests.unit.test_composition import FakeEmbeddings

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"
BOT = SRC / "entrypoints" / "bot.py"
COMPOSITION = SRC / "composition.py"

BASE = {
    "discord_token": "t",
    "discord_guild_id": 7,
    # Nothing listens on port 1: a read fails at once, which the live scope
    # absorbs, so the graph is built without a database.
    "database_url": "postgresql+asyncpg://u:p@127.0.0.1:1/none",
    "llm_api_key": "k",
    "indexed_channel_ids": "100",
}


def settings(**overrides: object) -> Settings:
    return Settings.model_validate({**BASE, **overrides})


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    )


def _calls(scope: ast.AST, func: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == func
    ]


class FakeChat:
    """A chat handle that is never asked anything while the graph is built."""

    async def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], schema_name: str
    ) -> JsonCompletion:
        raise AssertionError("assembly must not call the model")

    async def complete_text(self, system: str, user: str) -> TextCompletion:
        raise AssertionError("assembly must not call the model")

    async def complete_with_tools(
        self, system: str, user: str, tools: Sequence[ToolDefinition]
    ) -> ToolCompletion:
        raise AssertionError("assembly must not call the model")

    def tool_caller(self) -> ChatModel:
        return self


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """An engine pointed at nothing, disposed whether or not the test passes."""
    engine = create_async_engine(BASE["database_url"])
    try:
        yield engine
    finally:
        await engine.dispose()


def fake_edges(engine: AsyncEngine, width: int = 1536) -> Edges:
    return Edges(
        chat=FakeChat(),
        summary_chat=FakeChat(),
        embeddings=FakeEmbeddings(width),
        engine=engine,
    )


# --- production builds what it always built -----------------------------


def test_production_edges_are_the_components_the_process_always_had() -> None:
    config = settings()
    edges = Edges.production(config)

    assert isinstance(edges.chat, OpenAICompatibleChat)
    assert edges.chat.model == config.chat_model
    assert isinstance(edges.summary_chat, OpenAICompatibleChat)
    assert edges.summary_chat.model == config.extraction_model
    assert isinstance(edges.embeddings, OpenAICompatibleEmbeddings)
    assert edges.embeddings.dimensions == config.embedding_dimensions
    assert isinstance(edges.engine, AsyncEngine)
    assert edges.engine.url.render_as_string(hide_password=False) == BASE["database_url"]
    # Carried for the harness; production leaves both at what adapters use.
    assert edges.http_transport is None
    assert edges.clock is utc_now


def test_main_assembles_over_the_production_edges() -> None:
    main = _function(BOT, "main")
    [call] = _calls(main, "assemble")
    assert any(_calls(arg, "production") for arg in call.args), (
        "main must build the process over Edges.production, or production and "
        "the end-to-end harness run different graphs"
    )


def test_nothing_between_the_edges_builds_a_model_or_engine_of_its_own() -> None:
    """Each of these is an edge; built inside, a fake handed in is ignored.

    Only calls made directly in the two bodies are checked, and only for the
    model, embedding and engine edges; the transport and clock are checked
    where they are used, in `test_transport_and_clock_seam`.
    """
    stack = _function(COMPOSITION, "build_answer_stack")
    for edge in ("build_chat_model", "build_embeddings", "create_async_engine"):
        assert not _calls(stack, edge), f"build_answer_stack builds {edge} itself"
    body = _function(BOT, "assemble")
    for edge in (
        "production",
        "build_chat_model",
        "build_embeddings",
        "build_summary_model",
        "create_async_engine",
    ):
        assert not _calls(body, edge), f"assemble builds {edge} itself"


def test_assemble_hands_build_bot_every_collaborator() -> None:
    """A keyword left out of this call is a feature built and reached by nothing."""
    expected = set(inspect.signature(build_bot).parameters) - {"settings", "answers"}
    [call] = _calls(_function(BOT, "assemble"), "build_bot")
    passed = {k.arg for k in call.keywords}
    assert expected <= passed, f"assemble never passes {sorted(expected - passed)}"


# --- the graph, built over fakes ----------------------------------------


async def test_the_embedding_width_is_still_checked_against_the_edges(
    engine: AsyncEngine,
) -> None:
    with pytest.raises(ConfigurationError, match="reindex"):
        await build_answer_stack(settings(), edges=fake_edges(engine, width=768))


async def test_assemble_builds_the_whole_process_over_the_edges_it_is_given(
    engine: AsyncEngine,
) -> None:
    edges = fake_edges(engine)
    process = await assemble(settings(), edges)

    assert isinstance(process, Process)
    assert process.stack.engine is edges.engine
    assert process.stack.chat is edges.chat
    assert process.graph.asks is not None
    assert process.graph.indexing is not None
    assert process.graph.notifications is not None
    # A failed read keeps the environment's scope, which is the one at boot.
    assert set(process.scope.current()) == {100}
    # The summariser writes with the summary edge, not a handle built inside.
    assert process.conversations.summariser.model is edges.summary_chat
