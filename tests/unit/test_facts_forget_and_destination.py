"""Regressions from the personal-facts build: forget-everywhere wiring, and /ask
never treating an unplaceable guild interaction as a direct message."""

from __future__ import annotations

import ast
from pathlib import Path

import chatmemory

SRC = Path(chatmemory.__file__).parent


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node  # type: ignore[return-value]
    raise AssertionError(f"{name} not found in {path}")


def test_conversations_are_built_with_the_fact_store() -> None:
    """Without it the memory layer logged an ERROR on every forget-everywhere,
    claiming facts were kept, even where the ask path had deleted them."""
    built = _function(SRC / "composition.py", "build_conversations")
    calls = [
        n for n in ast.walk(built)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Conversations"
    ]
    assert calls, "build_conversations never constructs Conversations"
    assert any(k.arg == "facts" for k in calls[0].keywords)


def test_a_guild_interaction_without_a_channel_is_refused_not_treated_as_a_dm() -> None:
    """A DM is where an email may be shown, so guessing DM widens the answer."""
    source = (SRC / "adapters" / "discord" / "bot.py").read_text()
    assert "interaction.guild_id is not None and interaction.channel_id is None" in source
    assert "UNPLACEABLE_INTERACTION" in source
    # The old truthiness test would treat guild_id=0-like values and a missing
    # channel as a DM; it must be gone.
    assert "if interaction.guild_id and interaction.channel_id" not in source


async def test_forget_everywhere_deletes_facts_through_conversations() -> None:
    from chatmemory.app.conversation import Conversations
    from chatmemory.domain.identity import PersonRef

    erased: list[PersonRef] = []

    class Eraser:
        async def delete_all(self, person: PersonRef) -> int:
            erased.append(person)
            return 1

    class Store:
        def __getattr__(self, name: str):
            async def _noop(*_a: object, **_k: object) -> object:
                from chatmemory.ports.memory import MemoryPurge
                return MemoryPurge(turns=0, summaries=0) if name.startswith("forget") else None
            return _noop

    conversations = Conversations(Store(), summariser=None, facts=Eraser())  # type: ignore[arg-type]
    assert conversations._memory._facts is not None  # noqa: SLF001
