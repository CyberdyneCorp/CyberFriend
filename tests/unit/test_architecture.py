"""Structural invariants.

These assert properties of the code itself rather than its behaviour. Each one
guards a rule that is easy to state, easy to violate accidentally in a
refactor, and expensive to violate.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pkgutil
from pathlib import Path
from typing import get_type_hints

import pytest

import chatmemory
from chatmemory.domain import search as search_module
from chatmemory.ports.store import SearchBackend

SRC = Path(chatmemory.__file__).parent
PLATFORM_PACKAGES = {"discord", "openai", "asyncpg", "sqlalchemy", "mcp"}
CORE_LAYERS = ("domain", "ports")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def _core_modules() -> list[Path]:
    return [p for layer in CORE_LAYERS for p in (SRC / layer).rglob("*.py")]


def test_core_does_not_import_platform_libraries() -> None:
    """domain/ and ports/ must not know which platform or database is in use."""
    offenders = {
        str(path.relative_to(SRC)): sorted(_imports(path) & PLATFORM_PACKAGES)
        for path in _core_modules()
        if _imports(path) & PLATFORM_PACKAGES
    }
    assert not offenders, f"core layer imports platform libraries: {offenders}"


def test_app_layer_does_not_import_platform_libraries() -> None:
    offenders = {
        str(path.relative_to(SRC)): sorted(_imports(path) & PLATFORM_PACKAGES)
        for path in (SRC / "app").rglob("*.py")
        if _imports(path) & PLATFORM_PACKAGES
    }
    assert not offenders, f"app layer imports platform libraries: {offenders}"


def test_search_query_exposes_no_permission_field() -> None:
    """SearchQuery is rewritable by the corrective loop.

    If the readable-channel predicate were a field here, a "broaden the
    filters" action could widen permissions. `channels` is intent -- a
    preference the asker expressed -- and narrowing it can never grant access.
    """
    hints = get_type_hints(search_module.SearchQuery)
    forbidden = {"viewer", "visible_channels", "readable_channels", "permitted_channels",
                 "allowed_channels", "acl", "permissions"}
    assert not (set(hints) & forbidden), (
        f"SearchQuery must not carry authorization: {set(hints) & forbidden}"
    )


def test_search_query_is_immutable() -> None:
    """A rewriter must produce a new query rather than mutate a shared one."""
    assert search_module.SearchQuery.__dataclass_params__.frozen  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "method", ["search", "thread_context", "list_channels"]
)
def test_every_content_returning_method_requires_a_viewer(method: str) -> None:
    """Unfiltered retrieval must be unrepresentable, not merely untested."""
    signature = inspect.signature(getattr(SearchBackend, method))
    params = list(signature.parameters)
    assert "viewer" in params, f"{method} must take a viewer"
    viewer_param = signature.parameters["viewer"]
    assert viewer_param.default is inspect.Parameter.empty, (
        f"{method}'s viewer must be required, not defaulted"
    )


def test_viewer_has_no_default_visible_channels() -> None:
    """A viewer constructed without an explicit channel set must fail.

    Defaulting to empty fails safe but hides the bug; defaulting to all is the
    exfiltration bug itself. Requiring it surfaces the mistake at the callsite.
    """
    from chatmemory.domain.identity import Viewer

    fields = {f.name: f for f in dataclasses.fields(Viewer)}
    channels = fields["visible_channels"]
    assert channels.default is dataclasses.MISSING
    assert channels.default_factory is dataclasses.MISSING


def test_every_module_is_importable() -> None:
    """Catches a broken module that no test happens to import."""
    for info in pkgutil.walk_packages(chatmemory.__path__, "chatmemory."):
        __import__(info.name)
