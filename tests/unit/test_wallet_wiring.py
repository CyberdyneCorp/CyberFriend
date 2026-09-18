"""Wallet balances, reached from the process that actually runs.

This project's recurring failure is a finished module wired to nothing, and a
tool is the shape where it hides best: registered, offered and never called
looks exactly like a tool nobody needed.

The chain read from the bot down:

  build_federation -> settings.wallet_tools_enabled -> build_chain_tools
  -> merged into FederationConfig and chained into the session factory, so the
  loop is offered `chain_balances:wallet_balances` and the invoker mints its
  clearance.
"""

from __future__ import annotations

import ast
from pathlib import Path

from chatmemory.adapters.chain.provider import WALLET_TOOL, WalletProvider
from chatmemory.composition import chain_tools_config, federates_anything
from chatmemory.config import Settings

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"
COMPOSITION = SRC / "composition.py"

BASE = {
    "discord_token": "x",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "llm_api_key": "k",
}
ENABLED = BASE | {"wallet_tools_enabled": True, "infura_key": "k"}


def _function(path: Path, name: str) -> ast.AST:
    found = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name
    ]
    assert found, f"{path.name} has no function {name}"
    return found[0]


def _calls(scope: ast.AST, func: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == func
    ]


def test_the_federation_builds_the_wallet_tools() -> None:
    build = _function(COMPOSITION, "build_federation")
    assert _calls(build, "build_chain_tools"), (
        "without this the tool is never registered and the loop never sees it"
    )


def test_the_wallet_tools_are_merged_and_their_factory_chained() -> None:
    """Registering without chaining the factory offers a tool that nothing can
    open -- which fails at the socket rather than at a boundary."""
    source = COMPOSITION.read_text()
    assert "chain.merge_into(" in source
    assert "chain.factory(factory)" in source


def test_enabling_wallets_alone_is_enough_to_turn_federation_on() -> None:
    """The one predicate both the config and the proposer use. A third kind of
    tool added to one copy and not the other is a tool registered, offered and
    never called."""
    assert federates_anything(Settings(**ENABLED))  # type: ignore[arg-type]
    assert not federates_anything(Settings(**BASE))  # type: ignore[arg-type]


def test_the_operator_key_reaches_the_adapter() -> None:
    config = chain_tools_config(Settings(**ENABLED))  # type: ignore[arg-type]
    assert config.infura_key == "k"


def test_no_key_means_no_key_rather_than_an_empty_string() -> None:
    config = chain_tools_config(Settings(**BASE))  # type: ignore[arg-type]
    assert config.infura_key is None


def test_the_tool_is_named_the_same_everywhere() -> None:
    """The allowlist entry, the provider and the schema must agree, or the
    registry lists a tool the session refuses as unknown."""
    from chatmemory.adapters.chain.registration import ChainToolsConfig, build_chain_tools

    tools = build_chain_tools(ChainToolsConfig(infura_key="k"))
    assert tools.allowlist[0].tool == WALLET_TOOL
    assert tools.allowlist[0].server == WalletProvider.server
