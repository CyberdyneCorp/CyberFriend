"""Live scope and market data, reached from the processes that run.

Two finished modules were reachable from nothing: `LiveScope` was used by
ingest alone, so the bot and the MCP server answered from the environment's
startup set; and the market providers were built and tested and merged into
no federation. This file proves both chains from `entrypoints/` down -- by
reading the call chain where running a process is not practical, and by
driving the objects those calls build where it is.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import chatmemory.composition as composition
from chatmemory.adapters.market.registration import MarketTools, MarketToolsConfig
from chatmemory.app.ask import AskService
from chatmemory.app.authorization import ActionOrigin, InvocationRequest, ToolEffect
from chatmemory.app.egress import MARKET_CRYPTO_PROVIDER, MARKET_FX_PROVIDER
from chatmemory.app.scope import LiveScope, StaticScope
from chatmemory.composition import (
    build_ask_service,
    build_federation,
    build_federation_config,
    federates_anything,
)
from chatmemory.config import Settings
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.entrypoints import bot as bot_entrypoint
from chatmemory.entrypoints.bot import build_bot, run_beside_scope
from chatmemory.entrypoints.mcp_server import build_acl
from chatmemory.health import HealthState
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember
from tests.unit.test_market_tools import COINGECKO_OK, FakeMarket, answering

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"
BOT = SRC / "entrypoints" / "bot.py"
MCP = SRC / "entrypoints" / "mcp_server.py"
COMPOSITION = SRC / "composition.py"

BASE: dict[str, object] = {
    "discord_token": "t",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
    "indexed_channel_ids": "100",
}

ASKER = PersonRef("discord", 7)
ENV_CHANNEL = 100
ADDED_CHANNEL = 200


def settings(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


class MutableScope:
    """A scope provider whose value a test moves, as a refresh would."""

    def __init__(self, *ids: int) -> None:
        self.ids = frozenset(ids)

    def current(self) -> frozenset[int]:
        return self.ids


def guild() -> FakeGuild:
    return FakeGuild(
        members=[FakeMember(ASKER.platform_user_id)],
        text_channels=[
            FakeChannel(ENV_CHANNEL, public=True),
            FakeChannel(ADDED_CHANNEL, public=True),
        ],
    )


# --- AST helpers ---------------------------------------------------------------


def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text())
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    ]
    assert found, f"{path.name} has no function {name}"
    return found[0]


def _called(node: ast.Call) -> str | None:
    return getattr(node.func, "id", None) or getattr(node.func, "attr", None)


def _calls(scope: ast.AST, func: str, keyword: str | None = None) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and _called(node) == func
        and (keyword is None or any(k.arg == keyword for k in node.keywords))
    ]


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


# --- live scope: the bot ------------------------------------------------------


def test_bot_main_builds_refreshes_and_passes_a_live_scope() -> None:
    main = _function(BOT, "main")
    assert _calls(main, "build_live_scope"), "bot main never builds a LiveScope"
    assert _calls(main, "refresh"), "bot main must refresh scope before identifying"
    passed = _calls(main, "build_bot", "scope")
    assert passed, "bot main must hand build_bot scope=, or resolvers read the environment"


def test_bot_main_runs_the_refresh_loop_beside_the_gateway() -> None:
    main = _function(BOT, "main")
    runs = _calls(main, "run_beside_scope")
    assert runs, "bot main never runs the scope refresh loop"
    assert any(_calls(r, "start") for r in runs), (
        "the refresh loop must run beside client.start, not before or after it"
    )
    beside = _function(BOT, "run_beside_scope")
    assert _calls(beside, "scope_loop")


def test_build_bot_hands_scope_to_the_ask_service() -> None:
    assert _calls(_function(BOT, "build_bot"), "build_ask_service", "scope")


def test_both_bot_resolvers_are_built_over_the_one_scope() -> None:
    built = _function(COMPOSITION, "build_ask_service")
    for resolver in ("DiscordAclResolver", "DiscordAudienceResolver"):
        calls = _calls(built, resolver)
        assert calls, f"build_ask_service no longer builds {resolver}"
        assert all("indexed" in _names_in(c) for c in calls)


@pytest.mark.parametrize("path", [BOT, MCP], ids=lambda p: p.name)
def test_no_serving_main_reads_the_startup_scope(path: Path) -> None:
    """The attribute that was the bug. Only a no-database fallback may read it."""
    reads = [
        n for n in ast.walk(_function(path, "main"))
        if isinstance(n, ast.Attribute) and n.attr == "indexed_channel_ids"
    ]
    assert not reads, f"{path.name} reads settings.indexed_channel_ids"


async def test_a_channel_added_after_the_bot_is_built_is_readable() -> None:
    """Behavioural, through `build_bot`: the object the process runs."""
    scope = MutableScope(ENV_CHANNEL)

    class Answers:
        async def answer(self, question: object) -> object:
            raise AssertionError("not asked")

    graph = build_bot(settings(), Answers(), scope=scope)  # type: ignore[arg-type]
    fake = guild()
    graph.client.get_guild = lambda _id: fake  # type: ignore[assignment,method-assign,return-value]
    acl = graph.asks._acl
    audiences = graph.asks._audiences

    before = await acl.resolve_viewer(ASKER)
    assert before.visible_channels == {ChannelRef("discord", ENV_CHANNEL)}

    scope.ids = frozenset({ENV_CHANNEL, ADDED_CHANNEL})
    after = await acl.resolve_viewer(ASKER)
    assert ChannelRef("discord", ADDED_CHANNEL) in after.visible_channels
    private = await audiences.resolve_private(ASKER)
    assert ChannelRef("discord", ADDED_CHANNEL) in private.readable_channels

    scope.ids = frozenset({ADDED_CHANNEL})
    removed = await acl.resolve_viewer(ASKER)
    assert ChannelRef("discord", ENV_CHANNEL) not in removed.visible_channels


def test_without_a_scope_the_ask_service_uses_the_environment_set() -> None:
    from chatmemory.adapters.discord.acl import static_guild

    asks = build_ask_service(settings(), static_guild(FakeGuild()), object())  # type: ignore[arg-type]
    assert isinstance(asks, AskService)
    assert isinstance(asks._acl.scope, StaticScope)  # type: ignore[attr-defined]
    assert asks._acl.scope.current() == {ENV_CHANNEL}  # type: ignore[attr-defined]


class FailingStore:
    """A configuration store whose database is down."""

    async def load(self) -> Any:
        raise ConnectionError("database unavailable")


async def test_the_live_scope_the_bot_builds_keeps_its_value_when_refresh_fails() -> None:
    scope = LiveScope.from_settings(
        FailingStore(),  # type: ignore[arg-type]
        settings(),
        {"INDEXED_CHANNEL_IDS": "100"},
    )
    report = await scope.refresh()
    assert not report.applied
    assert scope.current() == {ENV_CHANNEL}


async def test_run_beside_scope_ends_the_loop_when_the_gateway_returns() -> None:
    scope = LiveScope.from_settings(
        FailingStore(), settings(), {}, interval=3600  # type: ignore[arg-type]
    )

    async def gateway() -> None:
        await asyncio.sleep(0)

    state = HealthState()
    await asyncio.wait_for(run_beside_scope(gateway(), scope, state), timeout=2)
    assert "indexing_scope" in state.details


async def test_run_beside_scope_fails_the_process_when_the_loop_dies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dying(_scope: object, _state: object) -> None:
        raise RuntimeError("loop died")

    monkeypatch.setattr(bot_entrypoint, "scope_loop", dying)
    scope = MutableScope()

    async def gateway() -> None:
        await asyncio.sleep(3600)

    with pytest.raises(RuntimeError, match="loop died"):
        await asyncio.wait_for(
            run_beside_scope(gateway(), scope, HealthState()),  # type: ignore[arg-type]
            timeout=2,
        )


# --- live scope: the MCP server ------------------------------------------------


def test_mcp_main_builds_refreshes_and_passes_a_live_scope() -> None:
    main = _function(MCP, "main")
    assert _calls(main, "build_live_scope"), "mcp main never builds a LiveScope"
    assert _calls(main, "refresh"), "mcp main must refresh scope before serving"
    acl = _calls(main, "build_acl")
    assert acl and len(acl[0].args) == 2, "build_acl must be handed the live scope"
    gathered = _calls(main, "gather")
    assert gathered and any(_calls(g, "scope_loop") for g in gathered), (
        "the scope refresh loop must run beside the gateway and the HTTP server"
    )


async def test_the_mcp_resolver_follows_the_scope_and_drops_stale_cache() -> None:
    scope = MutableScope(ENV_CHANNEL)
    graph = build_acl(settings(), scope)  # type: ignore[arg-type]
    fake = guild()
    graph.client.get_guild = lambda _id: fake  # type: ignore[assignment,method-assign,return-value]
    graph.liveness.mark_live()

    first = await graph.resolver.resolve_viewer(ASKER)
    assert first.visible_channels == {ChannelRef("discord", ENV_CHANNEL)}

    scope.ids = frozenset({ADDED_CHANNEL})
    # The cached viewer was resolved under the old scope and must not be served.
    second = await graph.resolver.resolve_viewer(ASKER)
    assert second.visible_channels == {ChannelRef("discord", ADDED_CHANNEL)}


# --- market data ----------------------------------------------------------------


def test_build_federation_merges_market_tools() -> None:
    federation = _function(COMPOSITION, "build_federation")
    assert _calls(federation, "build_market_tools"), "market tools are built nowhere"
    merged = [c for c in _calls(federation, "merge_into") if "market" in _names_in(c)]
    assert merged, "market tools are built but never merged into the federation config"
    chained = [c for c in _calls(federation, "factory") if "market" in _names_in(c)]
    assert chained, "market sessions are never opened: the factory is not chained"
    # And the running bot reaches build_federation, from main down.
    assert _calls(_function(COMPOSITION, "build_answer_stack"), "build_federation")
    assert _calls(_function(BOT, "main"), "build_answer_stack")


def test_market_data_is_off_unless_enabled() -> None:
    assert not settings().market_tools_enabled
    assert build_federation_config(settings()) is None
    assert federates_anything(settings(market_tools_enabled=True))


def test_blank_market_settings_fall_back_to_their_defaults() -> None:
    loaded = settings(
        market_tools_enabled="", market_max_calls_per_run=" ", market_timeout_seconds=""
    )
    assert loaded.market_tools_enabled is False
    assert loaded.market_max_calls_per_run == 3
    assert loaded.market_timeout_seconds == 8.0


@pytest.mark.parametrize("field", ["market_max_calls_per_run", "market_timeout_seconds"])
def test_a_market_budget_that_refuses_everything_refuses_to_boot(field: str) -> None:
    with pytest.raises(ValueError):
        settings(**{field: 0})


def _with_client(
    monkeypatch: pytest.MonkeyPatch, fake: FakeMarket
) -> list[MarketToolsConfig]:
    """Build market tools over a mock transport, recording the config composition gave."""
    real: Callable[..., MarketTools] = composition.build_market_tools
    seen: list[MarketToolsConfig] = []

    def build(config: MarketToolsConfig | None = None, **_: object) -> MarketTools:
        assert config is not None
        seen.append(config)
        return real(config, client=fake.client)

    monkeypatch.setattr(composition, "build_market_tools", build)
    return seen


async def test_enabled_market_tools_register_read_only_without_web_or_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    seen = _with_client(monkeypatch, fake)
    tools = await build_federation(
        settings(market_tools_enabled=True, market_max_calls_per_run=2)
    )

    assert tools is not None
    names = tools.federation.registration.names
    assert {f"{MARKET_CRYPTO_PROVIDER}:crypto_price", f"{MARKET_FX_PROVIDER}:convert"} <= names
    # No SerpApi key: no S&P 500 server, nothing to route to.
    assert not any(n.startswith("market_index:") for n in names)
    for tool in tools.federation.registration.tools:
        assert tool.permit.effect is ToolEffect.READ_ONLY
        assert not tool.permit.mutation_enabled
    assert seen[0].max_calls_per_run == 2
    await tools.federation.aclose()


async def test_the_serpapi_key_adds_the_index_alongside_the_web_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeMarket(answering(COINGECKO_OK))
    seen = _with_client(monkeypatch, fake)
    tools = await build_federation(
        settings(market_tools_enabled=True, web_tools_enabled=True, serpapi_key="key")
    )

    assert tools is not None
    servers = {t.server for t in tools.federation.registration.tools}
    assert "market_index" in servers
    assert servers & {"wikipedia", "google"}, "web tools must still register beside market"
    assert seen[0].serpapi_key == "key"
    await tools.federation.aclose()


async def test_an_eth_lookup_runs_through_the_composed_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole door: routing, authorization, the egress guard, the provider."""
    fake = FakeMarket(answering(COINGECKO_OK))
    _with_client(monkeypatch, fake)
    tools = await build_federation(settings(market_tools_enabled=True))
    assert tools is not None

    outcome = await tools.surface.invoke(
        InvocationRequest(
            requester=ASKER,
            question="what is the price of ether right now?",
            qualified_name=f"{MARKET_CRYPTO_PROVIDER}:crypto_price",
            arguments={"asset": "ETH"},
            origin=ActionOrigin.REQUESTER_REQUEST,
        )
    )
    assert outcome.invoked, outcome.detail
    assert "2,388.82 USD" in outcome.text
    assert len(fake.requests) == 1
    await tools.federation.aclose()


async def test_free_text_in_a_currency_never_leaves_the_composed_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeMarket(answering({}))
    _with_client(monkeypatch, fake)
    tools = await build_federation(settings(market_tools_enabled=True))
    assert tools is not None

    outcome = await tools.surface.invoke(
        InvocationRequest(
            requester=ASKER,
            question="convert 100 USD to BRL",
            qualified_name=f"{MARKET_FX_PROVIDER}:convert",
            arguments={"amount": 100, "from": "USD", "to": "BRL secret plans"},
            origin=ActionOrigin.REQUESTER_REQUEST,
        )
    )
    assert not outcome.invoked
    assert fake.requests == []
    await tools.federation.aclose()


def test_the_market_client_type_is_httpx() -> None:
    """Guards the monkeypatch above against silently testing a different client."""
    assert isinstance(FakeMarket(answering({})).client, httpx.AsyncClient)
