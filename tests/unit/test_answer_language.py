"""Answering in the language the question was asked in.

From a production reply: asked "Oque voce pode fazer? Quais as suas
funcionalidades e comandos ?" -- a whole sentence of Portuguese -- the
assistant answered in English, listed one command out of six, and named two
capabilities by their internal server identifiers.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chatmemory.app.language import Language, detect
from chatmemory.app.self_description import (
    ALWAYS_AVAILABLE,
    NOTIFICATIONS,
    SCHEDULED,
    describe_capabilities,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "chatmemory"
DISCORD_BOT = SRC / "adapters" / "discord" / "bot.py"

ALL_TOOLS = (
    "wikipedia:search",
    "serpapi:search",
    "chain_balances:wallet_balances",
    "context7:query-docs",
    "market_crypto:crypto_price",
    "market_fx:convert",
    "market_index:index_level",
)


# --- detection ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Oque voce pode fazer? Quais as suas funcionalidades e comandos ?",
        "qual o saldo da carteira 0xabc",
        "o que foi decidido sobre o deploy?",
        "resuma a conversa de ontem",
    ],
)
def test_portuguese_is_recognised(text: str) -> None:
    assert detect(text) is Language.PORTUGUESE


@pytest.mark.parametrize(
    "text",
    [
        "what can you do?",
        "what did people ask me to do today",
        "summarise the discussion in general yesterday",
        "give me the list of commands",
    ],
)
def test_english_is_recognised(text: str) -> None:
    assert detect(text) is Language.ENGLISH


@pytest.mark.parametrize("text", ["ok", "?", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", ""])
def test_nothing_to_go_on_is_unknown_rather_than_a_default(text: str) -> None:
    """A wrong guess is worse than no instruction: answering in the question's
    language is what the model does unprompted most of the time anyway."""
    assert detect(text) is Language.UNKNOWN
    assert not detect(text).known


@pytest.mark.parametrize(
    "text",
    [
        "o deploy do commit quebrou o build?",
        "qual o status do deploy",
        "o token expirou",
    ],
)
def test_a_technical_question_is_never_called_english_for_its_nouns(text: str) -> None:
    """"deploy", "commit", "token" and "build" are spelled the same in both
    languages and are most of what people type here. A detector weighing them
    would call every technical question English and answer a Portuguese team
    in English.

    UNKNOWN is an acceptable result and often the honest one: the caller then
    says nothing about language, and the model answers in the question's
    language as it usually would. Being *wrong* is what must not happen.
    """
    assert detect(text) is not Language.ENGLISH


# --- the capability reply -----------------------------------------------


def test_the_reply_is_written_in_portuguese_when_asked_in_portuguese() -> None:
    described = describe_capabilities(ALL_TOOLS, language=Language.PORTUGUESE)
    assert "Eu sou o CyberFriend" in described
    assert "Comandos:" in described
    assert "I'm CyberFriend" not in described


def test_the_reply_is_english_when_asked_in_english() -> None:
    described = describe_capabilities(ALL_TOOLS, language=Language.ENGLISH)
    assert "I'm CyberFriend" in described
    assert "Commands:" in described


def test_every_command_is_listed() -> None:
    """One command out of six was named. The other five existed and worked."""
    described = describe_capabilities(ALL_TOOLS)
    for command in ALWAYS_AVAILABLE:
        assert f"`/{command.name}`" in described


def test_a_switched_off_command_is_not_promised() -> None:
    """Listing a command Discord will not show is worse than omitting one."""
    without = describe_capabilities(ALL_TOOLS, commands=ALWAYS_AVAILABLE)
    with_it = describe_capabilities(ALL_TOOLS, commands=(*ALWAYS_AVAILABLE, NOTIFICATIONS))
    assert "/notifications" not in without
    assert "/notifications" in with_it


def test_capabilities_are_named_by_what_they_do_not_by_their_server() -> None:
    """The reply said "chain_balances, context7", which is the name of a
    server and not a thing anybody can ask for."""
    described = describe_capabilities(ALL_TOOLS)
    assert "chain_balances" not in described
    assert "wallet balances on Ethereum and Base" in described
    assert "serpapi" not in described
    assert "the web (Wikipedia, Google)" in described


def test_an_unknown_server_is_still_named_rather_than_dropped() -> None:
    """An honest identifier beats omitting a capability entirely."""
    described = describe_capabilities(("somethingnew:tool",))
    assert "somethingnew" in described


# --- the list cannot drift from what is registered ----------------------


def _keyword(call: ast.Call, arg: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == arg and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return None


def _registered_commands() -> set[str]:
    """Every slash command the Discord surface registers, as a person types it.

    Subcommands are qualified with their group, because `/schedule create` is
    what somebody types and `create` alone is not a command at all.
    """
    tree = ast.parse(DISCORD_BOT.read_text())
    # Which local names hold a Group, and what that group is called.
    groups: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "attr", None) == "Group"
            and isinstance(node.targets[0], ast.Name)
        ):
            name = _keyword(node.value, "name")
            if name:
                groups[node.targets[0].id] = name

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "command":
            continue
        owner = getattr(node.func.value, "id", None)
        prefix = f"{groups[owner]} " if owner in groups else ""
        name = _keyword(node, "name")
        if name:
            names.add(f"{prefix}{name}")
        elif not prefix:
            # `name=action.value` over the IndexAction enum.
            names.update({"index", "unindex"})
    return names


def test_the_described_commands_are_exactly_the_registered_ones() -> None:
    """A command added to the bot and forgotten here goes unmentioned for
    ever, which is how it came to list one of six."""
    described = {c.name for c in (*ALWAYS_AVAILABLE, NOTIFICATIONS, *SCHEDULED)}
    assert described == _registered_commands()
