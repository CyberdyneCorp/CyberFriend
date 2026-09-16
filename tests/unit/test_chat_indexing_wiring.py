"""`/index` and `/unindex`, reached from the bot process that runs.

This project's recurring failure is a finished module wired to nothing. This
file reads the chain from `entrypoints/bot.py` down -- `main` builds the
indexing stores and hands them to `build_bot`, which attaches the service to
the client whose `setup_hook` registers both commands -- and then drives the
object `build_bot` returns, so a change made through it reaches the resolvers
the same process answers questions with.
"""

from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from chatmemory.adapters.documents.store import PostgresDocumentStore
from chatmemory.adapters.store.admin_postgres import PostgresChangeRecord
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.admin.audit import InMemoryChangeRecord
from chatmemory.app.configuration import ConfigurationEditor
from chatmemory.app.indexing import IndexAction, IndexOutcome, IndexRequest
from chatmemory.app.scope import LiveScope
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef
from chatmemory.entrypoints.bot import IndexingStores, build_bot, build_indexing_stores
from tests.unit.test_chat_indexing import (
    ADMIN,
    BASE,
    DESIGN,
    ENV_CHANNEL,
    ENVIRON,
    Purge,
    guild,
    person,
)
from tests.unit.test_runtime_configuration import FakeConfigurationStore

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"
BOT = SRC / "entrypoints" / "bot.py"
CLIENT = SRC / "adapters" / "discord" / "bot.py"


def _function(path: Path, name: str) -> ast.AST:
    found = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    ]
    assert found, f"{path.name} has no function {name}"
    return found[0]


def _calls(scope: ast.AST, func: str, keyword: str | None = None) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == func
        and (keyword is None or any(k.arg == keyword for k in node.keywords))
    ]


def test_bot_main_builds_indexing_stores_and_hands_them_to_build_bot() -> None:
    main = _function(BOT, "main")
    passed = _calls(main, "build_bot", "indexing")
    assert passed, "bot main must pass indexing= to build_bot, or /index is unavailable"
    indexing = next(k.value for k in passed[0].keywords if k.arg == "indexing")
    assert _calls(indexing, "build_indexing_stores"), "indexing= must be the real stores"
    # The same LiveScope the process refreshes and resolves against.
    assert any(
        isinstance(arg, ast.Name) and arg.id == "scope"
        for call in _calls(indexing, "build_indexing_stores")
        for arg in call.args
    )


def test_build_bot_attaches_the_service_to_the_client() -> None:
    assert _calls(_function(BOT, "build_bot"), "attach_indexing")


def test_setup_hook_registers_both_commands() -> None:
    assert _calls(_function(CLIENT, "setup_hook"), "_build_indexing_command")
    assert {a.value for a in IndexAction} == {"index", "unindex"}


def test_the_stores_main_builds_are_the_existing_postgres_purges_and_record() -> None:
    engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/none")
    scope = LiveScope.from_settings(
        FakeConfigurationStore(), Settings(**BASE), ENVIRON  # type: ignore[arg-type]
    )
    stores = build_indexing_stores(engine, scope)

    assert stores.scope is scope
    assert isinstance(stores.record, PostgresChangeRecord)
    assert isinstance(stores.purges[0], PostgresStore)
    documents: object = getattr(stores.purges[1], "_documents", None)
    assert isinstance(documents, PostgresDocumentStore)


class Answers:
    async def answer(self, question: object) -> object:
        raise AssertionError("not asked")


async def test_a_channel_indexed_through_the_built_bot_is_retrievable_at_once() -> None:
    """Behavioural, through `build_bot`: the object the process runs."""
    config = FakeConfigurationStore()
    scope = LiveScope.from_settings(config, Settings(**BASE), ENVIRON)  # type: ignore[arg-type]
    await scope.refresh()
    record = InMemoryChangeRecord()
    purge = Purge()
    stores = IndexingStores(scope, ConfigurationEditor(config), record, (purge,))
    graph = build_bot(
        Settings(**BASE),  # type: ignore[arg-type]
        Answers(),  # type: ignore[arg-type]
        scope=scope,
        indexing=stores,
    )
    g = guild()
    graph.client.get_guild = lambda _id: g  # type: ignore[assignment,method-assign,return-value]
    graph.client.get_channel = g.get_channel  # type: ignore[assignment,method-assign]
    assert graph.indexing is not None
    assert graph.client._indexing is graph.indexing
    acl = graph.asks._acl
    design = ChannelRef("discord", DESIGN)

    assert design not in (await acl.resolve_viewer(person(ADMIN))).visible_channels

    result = await graph.indexing.handle(IndexRequest(person(ADMIN), DESIGN, IndexAction.INDEX))

    assert result.outcome is IndexOutcome.INDEXED
    assert design in (await acl.resolve_viewer(person(ADMIN))).visible_channels
    channel = g.get_channel(DESIGN)
    assert channel is not None and channel.sent, "no notice was posted in the channel"
    assert len(await record.recent()) == 1

    removed = await graph.indexing.handle(
        IndexRequest(person(ADMIN), DESIGN, IndexAction.UNINDEX)
    )

    assert removed.outcome is IndexOutcome.UNINDEXED
    assert design not in (await acl.resolve_viewer(person(ADMIN))).visible_channels
    assert purge.calls == [design]
    assert scope.current() == {ENV_CHANNEL}
