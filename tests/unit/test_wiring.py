"""The running processes must actually use what was built.

Every defect of this shape so far -- windowing, embeddings, reconciliation,
cache invalidation, the withheld notice -- was implemented, tested, marked
done, and unreachable in production. Unit tests that construct a collaborator
directly cannot see that; these assert the composition root wires it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import chatmemory

SRC = Path(chatmemory.__file__).parent


def _calls_with_keyword(path: Path, func: str, keyword: str) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == func and any(k.arg == keyword for k in node.keywords):
            return True
    return False


def test_ask_service_is_built_with_the_withheld_probe() -> None:
    """Otherwise the notice is structurally unable to fire.

    Retrieval is pre-scoped to asker INTERSECT audience, so nothing is dropped
    later for `enforce_audience` to report. Only the probe searches the gap.
    """
    assert _calls_with_keyword(
        SRC / "composition.py", "AskService", "withheld"
    ), "build_ask_service must pass withheld=, or the notice can never fire"


def test_the_bot_entrypoint_supplies_a_search_backend() -> None:
    assert _calls_with_keyword(
        SRC / "entrypoints" / "bot.py", "build_bot", "search"
    ), "build_bot must receive a search backend for the withheld probe"


def test_bot_does_not_use_the_stub_answer_service() -> None:
    assert "StubAnswerService" not in (SRC / "entrypoints" / "bot.py").read_text()
